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

- `app/` — FastAPI application: webhook router, user-facing API router (`/auth/*`, `/me`, installations/repos, `/chat`), `/health`, CORS, startup
- `app/auth/` — *(Phase 5)* GitHub OAuth code exchange, Redis session store, `get_current_user` dependency, `verify_installation_access` access check
- `app/workers/` — Celery app definition and task implementations (`index_repo`, `review_pr`, `analyze_issue`, `auto_pr`)
- `app/github/` — GitHub App token minting, HMAC verification, REST API helpers, repo file fetch (`files.py`: tree / blob / contents); *(Phase 5)* user-token OAuth exchange + `GET /user`/`GET /user/installations` helpers; *(Phase 6)* `pulls.py`: PR metadata + changed files fetch and posting a review (Reviews API)
- `app/db/` — SQLModel models, engine setup, session factory (`build_engine()` mints a throwaway engine per Celery task)
- `ai/` — AI foundation and all LangGraph graphs
- `ai/graphs/` — One file per feature graph: `chat.py`, `pr_review.py`, `issue_analysis.py`, `auto_pr.py`
- `ai/llm.py` — `init_chat_model` + embeddings singleton (Phase 2 ships `get_embeddings`/`make_embeddings`; chat model lands in Phase 3)
- `ai/vectorstore.py` + `ai/retriever.py` — `langchain_postgres.PGVector` (collection `code_chunks`, 1536-dim) + `delete_paths` + repo-scoped retriever
- `ai/indexing/` — plain async indexing pipeline (not a graph): `languages.py` (extension→language, indexable filter, grammar loader), `chunker.py` (Tree-sitter function/class chunking + deterministic `chunk_id`), `pipeline.py` (fetch → chunk → embed → upsert, status transitions, incremental delete)
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
Fetches the repo's custom rules (F7) and injects them into the agent's system prompt.

### Auto-PR (plan → generate → commit)
```
locate (agentic retrieval) ─▶ plan (FixPlan) ─▶ [fan-out per file] generate_file
   ─▶ commit (branch + create/update/delete) ─▶ open_pr (+ link comment on issue)
```
Fetches the repo's custom rules (F7) and injects them into the `plan`/`generate_file` prompts.

## Storage Model

- **Postgres (relational)**: `Installation`, `Repository`, `Rule`, `PullRequest`, `Issue` tables
  (`Rule` is **repo-scoped** — `repository_id` FK, *changed 2026-07-02 from the Phase 7
  installation-scoped model*; each repo owns its custom review rules); *(Phase 5)* `User` (durable identity: `github_id`, `login`, `avatar_url`); *(Phase 6)* `ChatThread` (ownership bridge: `thread_id UUID unique`, `user_id FK→User`, `repo` full-name string, `title` string, `created_at`, `updated_at`). Schema created via `metadata.create_all()` at startup — no migration tool in v1.
- **Postgres (pgvector)**: Code-chunk embeddings — `embedding vector(1536)` + metadata `{repo, path, name, chunk_type, language, start_line, end_line}` + page content. Deterministic id = `hash(repo + path + line-span)` for idempotent upsert. Retrieval always filters on `repo`.
- **Postgres (LangGraph checkpointer)**: `AsyncPostgresSaver` state for chat memory (keyed by `thread_id`) and durable auto-PR.
- **Redis**: Celery broker + result backend; GitHub installation token cache (with TTL); webhook delivery-id dedup keys; *(Phase 5)* user **sessions** (`session_token → {user_id, user_token, refresh_token, expires_at}`, with TTL) and a user-installations cache. User/refresh tokens live only here — never in the browser.

## Auth and Access Model

Two token types — the **dual-token model** (added in Phase 5 to serve the `../revet_fe`
web frontend):

- **Installation token** (server-to-server) — *the app acting*. RS256 App JWT → short-lived
  installation access tokens (cached in Redis). Used for **all repo work**: webhook
  handling, indexing, code retrieval, posting reviews. Unchanged from v1.
- **User access token** (user-to-server, via GitHub **OAuth**) — *the logged-in person*.
  Obtained by exchanging an OAuth `code` (`GITHUB_OAUTH_CLIENT_ID`/`_SECRET`). Used **only**
  to answer "who is this and which installations/repos can they access" (`GET /user`,
  `GET /user/installations`) and to authorize requests. Stored server-side in the Redis
  session — **never returned to the browser**.

Sessions & access control (Phase 5):
- **Lightweight GitHub-only identity** — no passwords, no separate accounts system. A
  `User` row holds durable identity; a **Redis session** (`session_token → {user_id,
  user_token, refresh_token, expires_at}`) is the live session.
- `get_current_user` dependency resolves the session token (cookie/Authorization) → Redis
  → `User` (+ cached user token); `401` if missing/invalid.
- Every installation/repo endpoint runs `verify_installation_access(user, installation_id)`
  (via `GET /user/installations`, user token, cached) → `403` if the user can't access it.
  `installation_id` is **never** trusted as a bare capability.
- **CORS** allows `FRONTEND_ORIGIN` **with credentials**.

Unchanged:
- Webhooks verified via `X-Hub-Signature-256` HMAC using `GITHUB_WEBHOOK_SECRET`; invalid → `401`.
- All GitHub repo API calls use the installation token scoped to the relevant installation.

## Backend API contract (web frontend)

User-facing REST added in Phase 5, consumed by `../revet_fe` (full contract in its
`context/github-integration.md`). All except `/auth/session` are session-gated; all
installation/repo routes are access-checked.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/auth/session` | Exchange OAuth `code` → user token, upsert `User`, create session, return `{session_token, user}` |
| `POST` | `/auth/logout` | Invalidate the Redis session |
| `GET`  | `/me` | Current user + their installations (`GET /user/installations`) |
| `GET`  | `/installations/{installation_id}/repositories` | Stored `Repository` rows joined with the user's live installation repos + indexing status; `?refresh=1` re-pulls live |
| `POST` | `/repos/{owner}/{repo}/index` | Enqueue the existing `index_repo` task |
| `GET`  | `/repos/{owner}/{repo}/index-status` | Current `indexing_status` (+ counts) |
| `POST` | `/chat` | Existing SSE endpoint — now session-gated; upserts a `ChatThread` row on new threads |
| `GET`  | `/repos/{owner}/{repo}/chat/threads` | List `ChatThread` rows for the authed user + repo (access-checked); ordered by `updated_at` desc |
| `GET`  | `/chat/threads/{thread_id}` | Return `[{role, content}]` messages for the thread — read back from LangGraph checkpointer; `403` if the thread doesn't belong to the authed user |
| `GET`  | `/repos/{owner}/{repo}/pulls` | List `PullRequest` rows (`kind=review`) for the repo (access-checked), `updated_at` desc → `[{pr_number, state, github_url, created_at, updated_at}]`. Read-only "Reviews" activity feed; the review body lives only on the GitHub PR (`github_url`) — the row stores no findings/title |
| `GET`  | `/repos/{owner}/{repo}/rules` | *(Phase 11)* List the repo's custom rules (access-checked) → `[{id, content, created_at, updated_at}]` |
| `POST` | `/repos/{owner}/{repo}/rules` | *(Phase 11)* Create a rule for the repo (access-checked) |
| `PUT`/`PATCH` | `/repos/{owner}/{repo}/rules/{rule_id}` | *(Phase 11)* Update a rule (access-checked; the rule must belong to the path repo) |
| `DELETE` | `/repos/{owner}/{repo}/rules/{rule_id}` | *(Phase 11)* Delete a rule (access-checked; rule must belong to the path repo) |

## Configuration (env vars)

`DATABASE_URL` · `REDIS_URL` · `OPENAI_API_KEY` · `LLM_MODEL` (e.g. `openai:gpt-4o`) ·
`EMBEDDING_MODEL` (default `text-embedding-3-small`) · `GITHUB_APP_ID` ·
`GITHUB_APP_PRIVATE_KEY` · `GITHUB_WEBHOOK_SECRET` · `LANGSMITH_TRACING` ·
`LANGSMITH_API_KEY` · `LANGSMITH_PROJECT` · `LANGSMITH_ENDPOINT` (region host,
default `https://api.smith.langchain.com`) · `ENVIRONMENT` · `LOG_LEVEL`

`configure_langsmith()` (`app/observability.py`) bridges these `LANGSMITH_*` settings
into `os.environ` at startup (FastAPI lifespan + Celery worker master) — pydantic-settings
loads `.env` into `Settings` only, but the LangChain/LangGraph tracer reads `os.environ`,
so without the bridge tracing never activates unless the vars are externally exported.

Phase 5 (Auth & Frontend API): `GITHUB_OAUTH_CLIENT_ID` · `GITHUB_OAUTH_CLIENT_SECRET`
(the OAuth secret — backend only, never exposed) · `FRONTEND_ORIGIN` (CORS allow-list,
credentialed) · `SESSION_TTL` (session lifetime).

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
12. *(Phase 5)* The installation token does all repo work; the user token is used **only**
    for identity and access decisions, and never leaves the backend (browser holds only an
    opaque session cookie).
13. *(Phase 5)* Every user-facing installation/repo endpoint runs `get_current_user` then
    `verify_installation_access` before acting — `installation_id` is never trusted as a
    bare capability. The OAuth client secret comes from env only and is never exposed.
14. *(Phase 6)* A `thread_id` is never trusted as a bare capability — `GET /chat/threads/{thread_id}`
    verifies `ChatThread.user_id == authed.user.id` before reading checkpointer state; `403` if it
    doesn't match. Thread title is derived from the first human message (truncated to 80 chars); no
    extra LLM call.
