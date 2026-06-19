# Architecture Context

## Stack

| Layer              | Technology                                         | Role                                                  |
| ------------------ | -------------------------------------------------- | ----------------------------------------------------- |
| Service runtime    | Python 3.13 + FastAPI + uvicorn                    | Webhook intake, `/chat` (SSE), `/health`              |
| Background jobs    | Celery + Redis                                     | Broker + result backend; indexing / review / issue / auto-PR |
| AI orchestration   | LangGraph                                          | `StateGraph`, `Send` fan-out, conditional edges, checkpointer, prebuilt ReAct |
| AI components      | LangChain                                          | `init_chat_model`, prompt templates, `with_structured_output`, `@tool`, `langchain-postgres` retriever |
| LLM                | OpenAI via `init_chat_model("openai:<model>")`     | Provider-swappable by changing one config string      |
| Embeddings         | OpenAI `text-embedding-3-small` (1536-dim)         | Codebase semantic search                              |
| Vector store       | pgvector (Postgres extension)                      | Embeddings live in the same Postgres — no separate vector service |
| Relational DB/ORM  | PostgreSQL + SQLModel                              | Tables via `metadata.create_all()`; no migration tool in v1 |
| Graph durability   | LangGraph `AsyncPostgresSaver`                     | Chat memory (keyed by `thread_id`) + durable auto-PR  |
| Code chunking      | Tree-sitter                                        | Function/class-aware semantic chunks                  |
| Source/events      | GitHub App                                         | App JWT → installation token; webhook events          |
| Observability      | LangSmith + eval harness                           | Auto-trace every graph run; LLM-as-judge evals        |
| Queue visibility   | Flower (optional)                                  | Inspect tasks/retries                                 |
| Backing services   | PostgreSQL (pgvector) + Redis via docker-compose   | Only two external services needed locally             |

## System Architecture

```
GitHub App ──webhooks──▶ FastAPI ──verify HMAC, dedup, enqueue──▶ Redis (Celery broker)
                          │  /chat (sync, streaming SSE)                    │ consume
                          │  /health                                         ▼
                          ▼                                          Celery worker(s)
                Postgres + pgvector                                  asyncio.run(graph.ainvoke)
                (relational data + code-chunk                              │
                 embeddings + LangGraph checkpointer)             LangGraph graphs
                                                                  (review / chat / issue / auto-pr)
                                                                         │
                                                            OpenAI  ·  GitHub REST
```

## System Boundaries

- `app/` — FastAPI application: webhook router, `/chat` endpoint, `/health`, startup
- `app/workers/` — Celery app definition and task implementations (`index_repo`, `review_pr`, `analyze_issue`, `auto_pr`)
- `app/github/` — GitHub App token minting, HMAC verification, REST API helpers
- `app/db/` — SQLModel models, engine setup, session factory
- `ai/` — AI foundation and all LangGraph graphs
- `ai/graphs/` — One file per feature graph: `chat.py`, `pr_review.py`, `issue_analysis.py`, `auto_pr.py`
- `ai/llm.py` — `init_chat_model` + embeddings singleton
- `ai/vectorstore.py` + `ai/retriever.py` — `langchain_postgres.PGVector` + repo-scoped retriever
- `ai/tools.py` — LangChain `@tool`s: `retrieve_code`, `read_file`, `grep_symbol`, `list_directory`, `get_file_tree`
- `ai/schemas.py` — Pydantic models for `with_structured_output`: `ReviewFinding`, `FixPlan`, `RelevanceGrade`
- `ai/checkpointer.py` — `AsyncPostgresSaver` factory
- `evals/` — Golden datasets + `run_eval.py` + LLM-as-judge evaluators

## Celery Task → Graph Mapping

| Task           | Trigger                              | Graph / action                        |
| -------------- | ------------------------------------ | ------------------------------------- |
| `index_repo`   | App installed / repo added / `push`  | Plain async pipeline (not a graph)    |
| `review_pr`    | `pull_request` opened/synchronized   | `ai/graphs/pr_review.py`              |
| `analyze_issue`| `issues` opened                      | `ai/graphs/issue_analysis.py`         |
| `auto_pr`      | issue labeled `auto-fix`             | `ai/graphs/auto_pr.py`                |

Chat is **not queued** — synchronous request/response via `/chat`.

## Graph Shapes

### Chat (corrective + agentic RAG)
```
retrieve ─▶ grade_documents ─┬─(relevant)─▶ generate ─▶ END
                             └─(weak)─────▶ rewrite_query ─▶ retrieve   (bounded ~2 loops)
```

### PR Review (multi-agent fan-out)
```
prepare ─▶ retrieve_context ─▶ [Send fan-out] ─▶ correctness_reviewer ┐
                                               security_reviewer       ├▶ aggregate ─▶ format_post
                                               quality_reviewer        │
                                               custom_rules_reviewer   ┘
```

### Issue Analysis (ReAct)
```
create_react_agent(tools=[retrieve_code, read_file, grep_symbol, list_directory, get_file_tree])
→ explore repo → emit structured suggestion → post comment
```

### Auto-PR (plan → generate → commit)
```
locate (agentic retrieval) ─▶ plan (FixPlan) ─▶ [fan-out per file] generate_file
   ─▶ commit (branch + create/update/delete) ─▶ open_pr (+ link comment on issue)
```

## Storage Model

- **Postgres (relational)**: `Installation`, `Repository`, `Rule`, `PullRequest`, `Issue` tables; schema created via `metadata.create_all()` at startup — no migration tool in v1.
- **Postgres (pgvector)**: Code-chunk embeddings — `embedding vector(1536)` + metadata `{repo, path, name, chunk_type, language, start_line, end_line}` + page content. Deterministic id = `hash(repo + path + line-span)` for idempotent upsert. Retrieval always filters on `repo`.
- **Postgres (LangGraph checkpointer)**: `AsyncPostgresSaver` state for chat memory (keyed by `thread_id`) and durable auto-PR.
- **Redis**: Celery broker + result backend; GitHub installation token cache (with TTL); webhook delivery-id dedup keys.

## Auth and Access Model

- No user auth in v1 (web frontend is out of scope).
- GitHub App identity via RS256 JWT → short-lived installation access tokens (cached in Redis).
- Webhooks verified via `X-Hub-Signature-256` HMAC using `GITHUB_WEBHOOK_SECRET`; invalid → `401`.
- All GitHub API calls use the installation token scoped to the relevant installation.

## Configuration (env vars)

`DATABASE_URL` · `REDIS_URL` · `OPENAI_API_KEY` · `LLM_MODEL` (e.g. `openai:gpt-4o`) ·
`EMBEDDING_MODEL` (default `text-embedding-3-small`) · `GITHUB_APP_ID` ·
`GITHUB_APP_PRIVATE_KEY` · `GITHUB_WEBHOOK_SECRET` · `LANGSMITH_TRACING` ·
`LANGSMITH_API_KEY` · `LANGSMITH_PROJECT` · `ENVIRONMENT` · `LOG_LEVEL`

## Invariants

1. Webhook handlers verify HMAC, dedup by delivery id, enqueue, and return `200` — no heavy work in the request path.
2. Chat (`/chat`) is the only synchronous AI path; all other AI work runs in Celery tasks.
3. Celery tasks run `asyncio.run(graph.ainvoke(...))` and create async clients inside the task to avoid cross-loop reuse (prefork workers).
4. Indexing uses deterministic chunk ids (`hash(repo + path + line-span)`) so re-indexing is always an upsert, never a duplicate.
5. On `push`, only changed file paths are re-indexed: delete old vector points for those paths, then re-chunk and upsert.
6. Retrieval is always repo-scoped via a metadata filter `{"repo": repo}` — never cross-repo.
7. Installation access tokens are minted in one place and cached in Redis with TTL; never re-minted per call.
8. Secrets (private key, API keys, webhook secret) come from env only; never logged or embedded in code.
9. Auto-PR graphs are checkpointed (`AsyncPostgresSaver`) for durability; generated PRs are never auto-merged.
10. All corrective/ReAct agent loops are bounded by a max-iteration guard to prevent runaway tool calls.
11. `metadata.create_all()` manages the schema at startup; no migration tool until the schema starts churning.
