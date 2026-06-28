# Progress Tracker

Update this file after every meaningful implementation change.

## Current Phase

Phase 5 — Complete

## Current Goal

Phase 6 — PR Review (multi-agent fan-out graph → posted review + activity row).

## Completed

- PRD reviewed and all context files populated.
- **Phase 0 — Scaffold** (2026-06-19)
  - `docker-compose.yml` — Postgres (pgvector/pgvector:pg16) + Redis (redis:7-alpine)
  - `.env.example` — all required env vars documented
  - `pyproject.toml` — full dependency list (fastapi, sqlmodel, asyncpg, psycopg, langchain, langgraph, langsmith, celery, httpx, pyjwt, tree-sitter)
  - `app/config.py` — `Settings` via pydantic-settings, loads from `.env`
  - `app/db/models.py` — `Installation`, `Repository`, `Rule`, `PullRequest`, `Issue` SQLModel tables + `IndexingStatus` / `PRKind` enums
  - `app/db/session.py` — async engine (asyncpg), `AsyncSessionLocal`, `create_db()`, `get_session()`
  - `app/main.py` — FastAPI app, lifespan runs `create_db()` at startup, `/health` endpoint
  - `ai/`, `ai/graphs/`, `evals/` — empty package stubs for later phases
  - LangSmith traces automatically when `LANGSMITH_TRACING=true` (env-only wiring)
- **Phase 1 — GitHub App + Webhooks** (2026-06-21)
  - `app/redis_client.py` — lazy singleton `redis.asyncio` client (token cache + delivery dedup)
  - `app/github/auth.py` — RS256 App JWT minting; `get_installation_token()` mints via GitHub API and caches in Redis with TTL derived from `expires_at` (token minting in one place, invariant #7)
  - `app/github/webhooks.py` — `POST /webhooks/github`: HMAC `X-Hub-Signature-256` verify first (→ `401`), Redis `SET NX` dedup on `X-GitHub-Delivery`, then dispatch + enqueue
    - `installation`/`installation_repositories` (created/added) → upsert `Installation` + `Repository` rows, enqueue `index_repo`
    - `push` → enqueue `index_repo` with changed paths; `pull_request` (opened/synchronize) → `review_pr`; `issues` (opened) → `analyze_issue`, (labeled `auto-fix`) → `auto_pr`
  - `app/workers/celery_app.py` — Celery app (Redis broker/backend, json serializers)
  - `app/workers/tasks.py` — stub tasks `index_repo`, `review_pr`, `analyze_issue`, `auto_pr` (log only; graphs land in later phases)
  - `app/main.py` — router wired via `include_router`
  - Verified end-to-end with TestClient: bad signature → `401`, valid PR webhook → `200` + `review_pr` enqueued, redelivered delivery-id deduped, unhandled event → `200`; RS256 JWT mint/verify round-trips
- **Phase 2 — Indexing** (2026-06-21)
  - `ai/llm.py` — `make_embeddings()` + cached `get_embeddings()` (`OpenAIEmbeddings`, `text-embedding-3-small`)
  - `ai/vectorstore.py` — `PGVector` factory (collection `code_chunks`, 1536-dim, `postgresql+psycopg://`), `get_vectorstore()` singleton for sync/chat, `delete_paths()` (raw SQL on `langchain_pg_embedding.cmetadata`) for incremental re-index
  - `ai/retriever.py` — `get_retriever(repo)` always passes `filter={"repo": repo}` (invariant #6)
  - `ai/indexing/languages.py` — extension/filename → language map (18 languages), skip dirs/lockfiles/>100 KB filter, lazy per-language `tree-sitter` grammar loader
  - `ai/indexing/chunker.py` — Tree-sitter function/class-aware chunking (per-language definition node types, module-glue grouping, decorated-def unwrap, oversized-chunk line splitting), whole-file fallback, deterministic `chunk_id = sha1(repo:path:start-end)`
  - `app/github/files.py` — default branch, recursive tree (indexable blobs), blob + contents fetch with base64 decode + binary/size guards
  - `ai/indexing/pipeline.py` — `run_index()`: status `INDEXING → COMPLETED|FAILED`, full index (tree → concurrent blob fetch → chunk → batched embed/upsert) and incremental (`delete_paths` changed paths → re-fetch survivors → upsert); builds own engine/embeddings/PGVector per run and disposes (prefork-safe)
  - `app/db/session.py` — `build_engine()` per-task engine factory; `index_repo` task now runs `asyncio.run(run_index(...))` with `autoretry_for`/`retry_backoff`
  - Grammars: standard `tree_sitter` + one `tree-sitter-<lang>` package per programming language (rejected the bundled `tree-sitter-language-pack==1.9.1` — its Python 3.14 binding diverges from py-tree-sitter)
  - Verified against live Postgres+pgvector with a fake embedder + mocked GitHub: chunker output across Python/Go/Rust/C/JSON/MD; idempotent deterministic-id upsert; repo-scoped retrieval filter; full + incremental index (delete-by-path, removed-file handling); status `NOT_STARTED→COMPLETED` and `→FAILED` (re-raised for Celery retry)
  - Enhancements adopted after reviewing an external indexing design: chunks are embedded as a context-enriched string (`embedding_text()`: `File: <path>` + `<type>: <name>` header + fenced code) rather than raw code, improving retrieval/grounding; `is_indexable` now skips minified bundles (`.min.` in filename). (Rejected from that design: Qdrant, Inngest, per-chunk embedding, frontend-POST status — all conflict with our mandated stack; the per-repo payload index is already provided by `langchain_postgres`' auto-created `ix_cmetadata_gin`.)

- **Phase 3 — AI Foundation** (2026-06-27)
  - `ai/llm.py` — added `make_chat_model()` + cached `get_chat_model(model=None)` (`init_chat_model`, provider-prefixed id from `settings.llm_model`); cache is keyed per model id so a cheaper id (e.g. `openai:gpt-4o-mini`) for graders/reviewers is a one-arg call
  - `ai/schemas.py` — Pydantic v2 `with_structured_output` shapes: `ReviewFinding` (severity `Literal`, confidence 0–1), `FixPlan` + `FixFile` (action `Literal[create|update|delete]`), `RelevanceGrade`
  - `ai/checkpointer.py` — `checkpointer()` returns the `AsyncPostgresSaver.from_conn_string(...)` async context manager; `_conn_string()` strips any SQLAlchemy `+driver` suffix (psycopg opens its own connection); caller enters it inside the task loop and runs `await saver.setup()` once
  - `ai/tools.py` — five LangChain `@tool`s + `CODEBASE_TOOLS`: `retrieve_code`, `read_file`, `grep_symbol`, `list_directory`, `get_file_tree`. Repo context (`repo`, `installation_id`, `ref`) is injected via `config["configurable"]` (a `RunnableConfig` param hidden from the model schema), never model-supplied — keeps retrieval repo-scoped (invariant #6). File/dir/tree reads use the installation token + GitHub REST at the configured ref (default branch when unset)
  - `app/github/files.py` — added `list_tree()` (all blob paths, unfiltered) and `list_dir()` (`(name, type)` entries; `[]` when path is a file/missing) for the navigation tools
  - `ai/vectorstore.py` — added `search_symbol()` (repo-scoped `ILIKE` over `cmetadata->>'name'`) backing `grep_symbol`
  - LangSmith tracing needs no code — already env-wired from Phase 0 (`LANGSMITH_TRACING=true`)
  - Verified by import: all modules load; tool schemas expose only model args (`config` hidden); `get_chat_model` builds `ChatOpenAI` (gpt-4o), caches per id, and supports `with_structured_output` + `bind_tools`; schemas validate

- **Phase 4 — Chat** (2026-06-27)
  - `ai/graphs/chat.py` — corrective + agentic RAG `StateGraph` matching the PRD shape: `retrieve → grade_documents → (relevant ▸ generate | weak ▸ rewrite_query → retrieve)`, then `generate → (tool_calls ▸ tools → generate | END)`. `grade_documents` uses `with_structured_output(RelevanceGrade)`; `generate` binds `[retrieve_code, read_file]` so it can read full files, not just chunks. State carries per-thread `messages` (memory), plus `query`/`rewrites` for the corrective loop.
  - **Two bounded loops** (invariant #10): corrective rewrite loop capped at `MAX_REWRITES=2` (on cap, answers with what it has); agentic generate tool loop capped at `MAX_TOOL_ROUNDS=3` (on cap, `generate` drops tools so the turn always ends with a streamable text answer — never a dangling tool call).
  - `ai/constants.py` — `MAX_REWRITES`, `MAX_TOOL_ROUNDS`, `GRADER_MODEL="openai:gpt-4o-mini"` (cheap model for grade/rewrite; generation uses default `settings.llm_model` = gpt-4o).
  - `ai/retriever.py` — extracted `format_doc(doc)` (path:line header + page content); `ai/tools.py` now reuses it (removed its private duplicate).
  - `ai/checkpointer.py` — added `setup_checkpointer()` (enters the saver context, runs idempotent `setup()` once at startup).
  - `app/chat.py` — `POST /chat`: resolves repo → GitHub installation id (404 if app not installed) before streaming, builds `config.configurable` (`thread_id`, `repo`, `installation_id`), resets `query`/`rewrites` per turn so a checkpointed prior turn never leaks its rewritten query, then streams `graph.astream(..., stream_mode="messages")` filtered to the `generate` node's text chunks as SSE (`data: {"delta": ...}` JSON frames, terminal `{"done": true}`). Graph builder is module-level; each request compiles it against a per-request `checkpointer()` (own Postgres connection — safe under concurrent `/chat`, one event loop).
  - `app/main.py` — included `chat_router`; lifespan now runs `setup_checkpointer()` after `create_db()`.
  - Verified: all modules import; graph compiles and wiring matches the spec (edges + conditional routes); `_route_after_grade` returns rewrite when weak & under cap, generate at cap / when relevant. End-to-end `ainvoke` with fake retriever + fake model exercised the relevant path (0 rewrites → answer), the weak path (exactly 2 rewrites → answer), and empty-docs (graded weak, bounded, then answer). SSE encoder JSON-escapes newlines so multi-line tokens stay one frame. (Live OpenAI/Postgres run deferred — same as prior phases.)

- **Phase 5 — Auth & Frontend API** (2026-06-27)
  - `app/config.py` — added `github_oauth_client_id`, `github_oauth_client_secret`
    (backend-only, never exposed), `frontend_origin` (CORS), `session_ttl`; documented in
    `env.template`.
  - `app/db/models.py` — `User` table (durable identity: `github_id` unique, `login`,
    `avatar_url`); user/refresh tokens deliberately **not** stored here (they live only in
    the Redis session, invariant #12).
  - `app/github/oauth.py` — user-to-server helpers (httpx): `exchange_code` /
    `refresh_user_token` (POST `github.com/login/oauth/access_token`, secret from env),
    `get_authenticated_user` (`GET /user`), `list_user_installations`
    (`GET /user/installations`), `list_installation_repositories`
    (`GET /user/installations/{id}/repositories`, paginated). Boundary shapes are Pydantic
    (`OAuthTokens`, `GitHubIdentity`, `GitHubInstallation`, `GitHubRepo`); `OAuthError` for
    a GitHub `error` response. `refresh_token`/`expires_in` default empty so it also works
    in the App's non-expiring-token mode.
  - `app/auth/sessions.py` — Redis session store: `create_session` (opaque
    `secrets.token_urlsafe` token → `{user_id, user_token, refresh_token}`, TTL
    `settings.session_ttl`), `get_session`, `update_session_tokens` (persist a refresh),
    `delete_session`. Tokens never leave the backend.
  - `app/auth/dependencies.py` — `get_current_user` (cookie `session` **or**
    `Authorization: Bearer` → Redis → `User`; `401` if missing/invalid) returning an
    `AuthedUser`; `user_installations` (cached in Redis, `USER_CACHE_TTL=300`);
    `verify_installation_access` (`403` unless the GitHub installation id is in the user's
    list — invariant #13); `call_with_refresh` (runs a user-token call, refreshes the token
    once on a `401` and persists it, else surfaces the `401` as a re-login).
  - `app/api.py` — user-facing router (separate from the webhook router): `POST /auth/session`
    (exchange → upsert `User` → create session → `{session_token, user}`; only the opaque
    token reaches the browser), `POST /auth/logout`, `GET /me`,
    `GET /installations/{installation_id}/repositories` (access-checked; the user's live repos
    joined with stored `Repository.indexing_status`, `NOT_STARTED` when unindexed; `?refresh=1`
    bypasses the Redis repo cache), `POST /repos/{owner}/{repo}/index` (access-checked;
    enqueues the existing `index_repo`), `GET /repos/{owner}/{repo}/index-status`
    (access-checked; `indexing_status` + `chunk_count`).
  - `ai/vectorstore.py` — added `count_chunks(store, repo)` (repo-scoped `count(*)` over
    `langchain_pg_embedding`) backing index-status.
  - `app/chat.py` — `/chat` now depends on `get_current_user` and runs
    `verify_installation_access` on the resolved installation before streaming
    (session-gated + access-checked, invariant #13).
  - `app/main.py` — `CORSMiddleware` (allow `FRONTEND_ORIGIN`, credentials on, configured
    once) + `include_router(api_router)`.
  - Verified by import (all modules load, 8 user-facing routes register) and with mocked
    GitHub: session create/get/update/delete; access check allows owned installation,
    caches the installations list (one GitHub call), `403`s a foreign one; `call_with_refresh`
    refreshes once on a `401` and retries with the new token; full `TestClient` flow —
    gated route `401` without a session, `POST /auth/session` returns `{session_token, user}`
    with **no user/refresh token in the body**, `GET /me` lists installations, `POST /auth/logout`
    → subsequent `/me` `401`. (Live GitHub OAuth/Postgres run deferred — App needs OAuth
    enabled + a registered client secret, same as prior phases.)
  - Note: appended placeholder `GITHUB_OAUTH_CLIENT_ID`/`_SECRET`/`FRONTEND_ORIGIN`/`SESSION_TTL`
    to the local `.env` so the service boots; real values must be filled before live OAuth.

## In Progress

- None.

## Observability — LangSmith wiring (2026-06-28)

- `app/observability.py` — `configure_langsmith()` bridges the `LANGSMITH_*` settings into
  `os.environ` so LangChain/LangGraph auto-trace every graph run. Needed because
  pydantic-settings loads `.env` into the `Settings` object only, while the LangSmith SDK
  reads `os.environ` directly — without the bridge, tracing never activated unless the vars
  were externally exported. Still env-only / no `@traceable` wrappers (per code-standards).
- `app/config.py` — added `langsmith_endpoint` (region host; was present in `.env` but
  unmodeled and silently dropped).
- Wired at both entrypoints: FastAPI lifespan (`app/main.py`) and the Celery worker master
  (`app/workers/celery_app.py`, before prefork so children inherit `os.environ`).
- `env.template` — documented `LANGSMITH_ENDPOINT` + the bridge.

## Next Up

1. **Phase 6 — PR Review**
2. **Phase 7 — Issue Analysis**
3. **Phase 8 — Auto-PR**
4. **Phase 9 — Evals**
5. **Phase 10 — Polish**

## Open Questions

- **LLM model choice**: *Decided for chat (Phase 4)* — generation uses the default
  `settings.llm_model` (gpt-4o); the document grader and query rewriter use the cheaper
  `GRADER_MODEL=openai:gpt-4o-mini` (`ai/constants.py`). Still open for the PR-review
  reviewers (Phase 6) — apply the same split (cheap per-reviewer model, capable aggregator)
  unless evals show otherwise.
- **Migrations**: v1 uses `create_all()`; note when the schema starts churning so we can
  adopt Alembic at the right time.
- **Embedding dimensions**: PRD specifies `text-embedding-3-small` (1536-dim). If we
  switch embedding models, the vector column dimension must change — this is a breaking
  schema change.
- **GitHub App registration**: App ID and private key must be registered in GitHub before
  Phase 1 can be end-to-end tested.
- **OAuth config (Phase 5)**: the GitHub App needs *"Request user authorization (OAuth)
  during installation"* enabled, a Callback URL, and an OAuth client secret
  (`GITHUB_OAUTH_CLIENT_ID`/`GITHUB_OAUTH_CLIENT_SECRET`) before auth can be tested.
- **User-token lifetime (Phase 5)**: *Implemented (default).* `call_with_refresh`
  (`app/auth/dependencies.py`) refreshes the user token once on a `401` from a user-token
  call and persists it to the session; with no refresh token (the App's non-expiring mode)
  the `401` surfaces as a re-login. Works either way — confirm the App's token-expiry
  setting when OAuth is registered.
- **Session/user-token storage (Phase 5)**: *Decided & implemented.* Sessions live in
  **Redis** keyed by an opaque `session_token`; the `User` row holds only durable identity
  (`github_id`, `login`, `avatar_url`). Refresh tokens live **in the Redis session**, not on
  the `User` row — a session is the unit of token lifetime, and keeping tokens out of the
  relational row avoids encrypted-at-rest token columns (invariant #12). Revisit only if
  cross-session token reuse is ever needed.
- **Callback placement (Phase 5)**: *Backend-agnostic.* `get_current_user` reads the session
  token from either a `session` cookie or `Authorization: Bearer`, so both the
  forward-the-code (frontend owns the cookie) and shared-parent-domain (backend sets the
  cookie) deploys work without backend changes. Still a frontend/deploy decision for where the
  cookie is set.
- **Tool vectorstore reuse under Celery**: `retrieve_code`/`grep_symbol` use the cached
  `get_vectorstore()` singleton (async engine). This is correct for `/chat` (synchronous,
  one event loop) — *confirmed in Phase 4*. For the Celery graph tools (issue/PR/auto-PR, each `asyncio.run()`), the
  module-level async engine can bind to a closed loop (invariant #3) — when those graphs land
  (Phase 6–8) they must inject a per-task store/retriever via `config["configurable"]` or
  build one per run, like `run_index` does. Tools already read everything else from config, so
  this is an additive change with no tool-signature churn.

## Architecture Decisions

- **Access check is a plain function, not a FastAPI dependency** — `verify_installation_access`
  is `await`-ed inside each handler rather than wired as `Depends`, because the
  `installation_id` comes from a path param on `/installations/{id}/...` but is *derived from
  the repo* (DB lookup) on `/repos/{owner}/{repo}/...` and `/chat`. A single uniform
  dependency can't cover both sources, so the check stays an explicit first line in every
  installation/repo handler (still uniform, still invariant #13). Only `get_current_user`
  (no parameters) is a dependency. (Phase 5)
- **Indexing is a plain async pipeline, not a LangGraph graph** — no reasoning step
  needed; adding a graph would be over-engineering. (PRD §F2)
- **Single Postgres for everything** — relational data, pgvector embeddings, and
  LangGraph checkpointer all share one Postgres instance. Simplifies local dev and
  avoids a separate vector service. (PRD §3)
- **Chat is synchronous, not queued** — latency matters; user is waiting. All other AI
  work is async Celery tasks. (PRD §4.2)
- **Chat checkpointer is per-request, graph builder is module-level** — `build_chat_graph()`
  builds the uncompiled graph once; each `/chat` request enters its own
  `checkpointer()` context (its own Postgres connection) and compiles the builder against
  it. Avoids sharing one psycopg connection across concurrent requests while keeping chat
  memory durable per `thread_id`. A connection pool is a possible later optimization if
  per-request connect latency matters. (Phase 4)
- **Chat streaming filters by graph node** — `/chat` streams `stream_mode="messages"` and
  emits only the `generate` node's text chunks, so grader/rewrite LLM tokens and the
  generate loop's tool-call chunks (empty content) never reach the client. Each turn ends
  with a real text answer because `generate` drops its tools once `MAX_TOOL_ROUNDS` is hit.
  (Phase 4)
- **Deterministic chunk ids** = `hash(repo + path + line-span)` — enables idempotent
  upsert so re-indexing is safe. (PRD §F2)
- **Standard `tree_sitter` + per-language grammar packages** — one
  `tree-sitter-<lang>` package per programming language driving the standard
  `Parser`/`Language`/`node.type` API, rather than the bundled
  `tree-sitter-language-pack`. The latter (v1.9.1) ships its own native binding
  whose API (`node.kind`, callable `root_node`) diverges from py-tree-sitter on
  Python 3.14, so it was rejected. Data/markup languages have no grammar and use
  whole-file chunks. (architecture.md §Boundaries)
- **Per-task engine/clients in the indexing pipeline** — `run_index` builds its
  own DB engine (`build_engine()`), embeddings, and PGVector per invocation and
  disposes them, because Celery prefork tasks call `asyncio.run()` per run and the
  module-level async engine would bind connections to a closed loop. (invariant #3)
- **DB-enforced timestamps** — `created_at`/`updated_at` are `timestamptz` with
  `server_default=now()`; `updated_at` bumped by a Postgres `BEFORE UPDATE` trigger.
  Surfaced during Phase 1 live testing (asyncpg rejected aware datetimes against the
  original naive columns). The DB owns timestamp values, not the ORM. (architecture.md
  §Storage Model)
- **Web frontend added; auth is now Phase 5** *(2026-06-27, supersedes "no web frontend
  in v1")* — a Next.js frontend (`../revet_fe`) now consumes the backend. To serve it the
  backend gains a **user-auth layer**: GitHub **OAuth** (the GitHub App's user-to-server
  tokens) backed by a **Redis session**, plus session-gated, access-checked user-facing
  REST endpoints (`/auth/*`, `/me`, `/installations/.../repositories`, repo index/status,
  and a gated `/chat`). This is **lightweight GitHub-only identity** — no passwords, no
  separate accounts system. The **dual-token model** is the core idea: the existing
  **installation token** still does all repo work; the new **user token** only answers
  "who is looking and what can they access" and authorizes requests. The GitHub App must
  enable *"Request user authorization (OAuth) during installation"* and have an OAuth
  client secret. Full contract: `revet_fe/context/github-integration.md`.

## Session Notes

- PRD is at `GENAI_PRD.md` in the repo root.
- No code has been written yet; implementation starts with Phase 0.
- All context files have been populated from the PRD as of 2026-06-19.
