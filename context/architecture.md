# Architecture

## Stack

Python 3.13 · FastAPI · Celery + Redis · LangGraph + LangChain · OpenAI (`init_chat_model`,
`text-embedding-3-small`, 1536-dim) · Postgres + pgvector · SQLModel (`create_all`, no
migrations yet) · LangGraph `AsyncPostgresSaver` · Tree-sitter · LangSmith.
Local services come from docker-compose and are just Postgres and Redis.

```
GitHub ──webhooks──▶ FastAPI ──verify, dedup, enqueue──▶ Redis ──▶ Celery worker
                      │ /chat (SSE), REST API                     asyncio.run(graph.ainvoke)
                      ▼                                                  │
            Postgres: tables + pgvector chunks + checkpointer ◀──────────┘
                                                        OpenAI · GitHub REST
```

## Layout

- `app/main.py`: FastAPI app, lifespan (`create_db`, checkpointer setup, LangSmith env bridge), CORS
- `app/github/webhooks.py`: webhook router. `auth.py` handles token minting. `oauth.py` holds user-token helpers.
  `files.py`, `pulls.py`, `issues.py` and `git.py` are REST helpers.
- `app/api.py`: user-facing REST. `app/chat.py`: `/chat` SSE.
- `app/auth/`: Redis sessions, `get_current_user`, `verify_installation_access`, `call_with_refresh`
- `app/workers/`: Celery app and tasks (`index_repo`, `review_pr`, `analyze_issue`, `auto_pr`)
- `app/db/`: models, engine, `build_engine()` (a per-task engine)
- `ai/`: `llm.py`, `vectorstore.py`, `retriever.py`, `tools.py`, `schemas.py`, `prompts.py`,
  `constants.py`, `rules.py`, `checkpointer.py`
- `ai/indexing/`: languages, chunker, pipeline
- `ai/graphs/`: one file per feature: `chat.py`, `pr_review.py`, `issue_analysis.py`, `auto_pr.py`

## Triggers

| Event | Task | Runs |
|-------|------|------|
| App installed / repo added / `push` | `index_repo` | `ai/indexing/pipeline.py` |
| `pull_request` opened/synchronize | `review_pr` | `ai/graphs/pr_review.py` |
| `issues` opened | `analyze_issue` | `ai/graphs/issue_analysis.py` |
| `issues` labeled `auto-fix` | `auto_pr` | `ai/graphs/auto_pr.py` |
| `POST /chat` | none (synchronous) | `ai/graphs/chat.py` |

## Graphs

- **Chat**: `retrieve → grade → (relevant: generate | weak: rewrite → retrieve)`. `generate` can
  call `retrieve_code`/`read_file`. Rewrites are capped at 2 and tool rounds at 3. Only `generate`
  node tokens are streamed.
- **PR review**: `prepare → retrieve_context → Send fan-out to review ×N → aggregate → format_post`.
  It is one parametrized `review` node per perspective, and custom-rules runs only when rules exist.
  `aggregate` is plain Python (dedupe, drop low confidence, rank, cap). The output is one `COMMENT` review.
- **Issue analysis**: `prepare → agent ↔ tools → format_post`, capped at 6 tool rounds.
- **Auto-PR**: `locate → plan (FixPlan) → Send fan-out to generate_file → commit → open_pr`, or
  `no_fix` if the plan is empty. It makes one commit on `ai-fix/issue-<n>` via the Git Data API.

## Storage

- **Tables**: `User`, `Installation`, `Repository`, `Rule` (per repo), `PullRequest`
  (`kind` = review | auto-pr), `Issue`, `ChatThread` (maps `thread_id` to a user and repo).
  The DB owns timestamps: a `timestamptz` server default plus an `updated_at` trigger.
- **pgvector**: collection `code_chunks`. Each row holds an embedding plus
  `{repo, path, name, chunk_type, language, start_line, end_line}`.
- **Checkpointer**: chat memory per `thread_id`, plus run state for the Celery graphs.
- **Redis**: Celery broker, installation-token cache, webhook dedup keys, user sessions,
  and the user-installations cache.

## Auth

Dual-token model:

- **Installation token** (App JWT → installation token, cached in Redis). It does all repo work.
- **User token** (GitHub OAuth). It is used only for identity and access checks. It lives in the
  Redis session and never reaches the browser, which holds only an opaque session token.

`get_current_user` reads the session from a `session` cookie or `Authorization: Bearer`.
`verify_installation_access` is awaited inside each handler and is not a `Depends`, because the
installation id comes from a path param on some routes and is looked up from the repo on others.

## REST API

All routes except `/auth/session` need a session, and all repo/installation routes are access-checked.

`POST /auth/session` · `POST /auth/logout` · `GET /me` ·
`GET /installations/{id}/repositories` · `POST /repos/{o}/{r}/index` ·
`GET /repos/{o}/{r}/index-status` · `POST /chat` · `GET /repos/{o}/{r}/chat/threads` ·
`GET /chat/threads/{thread_id}` · `GET /repos/{o}/{r}/pulls` · `GET /repos/{o}/{r}/issues`

The full frontend contract is in `../revet_fe/context/github-integration.md`.

## Configuration

See `env.template`. `configure_langsmith()` copies `LANGSMITH_*` settings into `os.environ`
at startup (FastAPI and the Celery master). The tracer only reads `os.environ`.

## Invariants

1. Webhooks verify HMAC first, dedup by delivery id, enqueue, then return `200`. No heavy work happens in the request.
2. `/chat` is the only synchronous AI path.
3. Celery tasks run `asyncio.run(...)` and build their own engine, vector store, chat models and
   Redis client per run, and dispose them in `finally` (`close_redis()`). Module-level async
   singletons bind to a dead loop under prefork. Per-run objects are passed to graphs via
   `config["configurable"]`.
4. Chunk id = `sha1(repo:path:start-end)`, so re-indexing is an upsert. On `push`, delete the
   changed paths and re-index them.
5. Every retrieval is filtered by `{"repo": repo}`. Repo context comes from `configurable`,
   never from the model.
6. Installation tokens are minted in one place (`app/github/auth.py`) and cached.
7. Every agent loop has a max-iteration guard.
8. User tokens, OAuth secrets and installation tokens never leave the backend or reach logs.
9. A bare `installation_id`, `thread_id` or `rule_id` is never trusted. Always check ownership.
10. Generated PRs are never auto-merged.
11. Adopt Alembic before the first schema change that must preserve rows in a live DB.
    Changing the embedding model changes the vector dimension, which breaks the schema.
