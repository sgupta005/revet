# Progress Tracker

Update this file after every meaningful implementation change.

## Current Phase

Phase 3 — Complete

## Current Goal

Phase 4 — Chat

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

## In Progress

- None.

## Next Up

1. **Phase 4 — Chat**
2. **Phase 5 — PR Review**
3. **Phase 6 — Issue Analysis**
4. **Phase 7 — Auto-PR**
5. **Phase 8 — Evals**
6. **Phase 9 — Polish**

## Open Questions

- **LLM model choice**: PRD defaults to `openai:gpt-4o` for the chat model; should
  cheaper models (e.g. `gpt-4o-mini`) be used for graders and individual reviewers to
  reduce cost? Document the decision when made.
- **Migrations**: v1 uses `create_all()`; note when the schema starts churning so we can
  adopt Alembic at the right time.
- **Embedding dimensions**: PRD specifies `text-embedding-3-small` (1536-dim). If we
  switch embedding models, the vector column dimension must change — this is a breaking
  schema change.
- **GitHub App registration**: App ID and private key must be registered in GitHub before
  Phase 1 can be end-to-end tested.
- **Tool vectorstore reuse under Celery**: `retrieve_code`/`grep_symbol` use the cached
  `get_vectorstore()` singleton (async engine). This is correct for `/chat` (synchronous,
  one event loop). For the Celery graph tools (issue/PR/auto-PR, each `asyncio.run()`), the
  module-level async engine can bind to a closed loop (invariant #3) — when those graphs land
  (Phase 5–7) they must inject a per-task store/retriever via `config["configurable"]` or
  build one per run, like `run_index` does. Tools already read everything else from config, so
  this is an additive change with no tool-signature churn.

## Architecture Decisions

- **Indexing is a plain async pipeline, not a LangGraph graph** — no reasoning step
  needed; adding a graph would be over-engineering. (PRD §F2)
- **Single Postgres for everything** — relational data, pgvector embeddings, and
  LangGraph checkpointer all share one Postgres instance. Simplifies local dev and
  avoids a separate vector service. (PRD §3)
- **Chat is synchronous, not queued** — latency matters; user is waiting. All other AI
  work is async Celery tasks. (PRD §4.2)
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
- **No web frontend in v1** — webhooks are HMAC-verified; the AI engine works
  standalone. UI is an explicit later phase. (PRD §1)

## Session Notes

- PRD is at `GENAI_PRD.md` in the repo root.
- No code has been written yet; implementation starts with Phase 0.
- All context files have been populated from the PRD as of 2026-06-19.
