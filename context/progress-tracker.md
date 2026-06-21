# Progress Tracker

Update this file after every meaningful implementation change.

## Current Phase

Phase 1 — Complete

## Current Goal

Phase 2 — Indexing

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

## In Progress

- None.

## Next Up

1. **Phase 2 — Indexing**
   - Tree-sitter chunker; OpenAI embeddings; pgvector upsert (deterministic ids)
   - `index_repo` task: status transitions `NOT_STARTED → INDEXING → COMPLETED | FAILED`
   - Incremental re-index on push (delete changed paths, re-chunk); repo-scoped retriever
2. **Phase 3 — AI Foundation**
3. **Phase 4 — Chat**
4. **Phase 5 — PR Review**
5. **Phase 6 — Issue Analysis**
6. **Phase 7 — Auto-PR**
7. **Phase 8 — Evals**
8. **Phase 9 — Polish**

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
