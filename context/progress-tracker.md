# Progress Tracker

Update this file after every meaningful implementation change.

## Current Phase

Phase 0 — Complete

## Current Goal

Phase 1 — GitHub App + Webhooks

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

## In Progress

- None.

## Next Up

1. **Phase 0 — Scaffold**
   - `docker-compose.yml` with Postgres (pgvector) + Redis services
   - `.env.example` with all required env vars
   - `app/config.py` — settings loaded from env (`DATABASE_URL`, `REDIS_URL`, `OPENAI_API_KEY`, `LLM_MODEL`, `EMBEDDING_MODEL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `ENVIRONMENT`, `LOG_LEVEL`)
   - `app/db/models.py` — SQLModel models: `Installation`, `Repository`, `Rule`, `PullRequest`, `Issue`
   - `app/db/session.py` — engine + session factory + `create_all()` on startup
   - `app/main.py` — FastAPI app with `/health` endpoint and startup event
   - LangSmith env var wiring (auto-traces when `LANGSMITH_TRACING=true`)

2. **Phase 1 — GitHub App + Webhooks**
   - `app/github/auth.py` — App JWT (RS256) → installation token, Redis cache
   - `app/github/webhooks.py` — HMAC verify (`X-Hub-Signature-256`), webhook router
   - `app/workers/celery_app.py` — Celery app definition
   - `app/workers/tasks.py` — stub tasks: `index_repo`, `review_pr`, `analyze_issue`, `auto_pr`
   - Delivery-id dedup via Redis

3. **Phase 2 — Indexing**
4. **Phase 3 — AI Foundation**
5. **Phase 4 — Chat**
6. **Phase 5 — PR Review**
7. **Phase 6 — Issue Analysis**
8. **Phase 7 — Auto-PR**
9. **Phase 8 — Evals**
10. **Phase 9 — Polish**

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
- **No web frontend in v1** — webhooks are HMAC-verified; the AI engine works
  standalone. UI is an explicit later phase. (PRD §1)

## Session Notes

- PRD is at `GENAI_PRD.md` in the repo root.
- No code has been written yet; implementation starts with Phase 0.
- All context files have been populated from the PRD as of 2026-06-19.
