# AI Workflow Rules

## How to work

- Build one phase at a time (see the roadmap in `project-overview.md`). Each phase should work
  end-to-end before the next starts.
- Make small, verifiable changes. Split any change that mixes AI graph work with webhook/Celery
  work, or that touches more than one feature graph.
- Don't invent product behavior. If something is ambiguous, ask the user or settle it in the
  relevant context file first.

## Don't change without asking

- The Celery `asyncio.run(...)` pattern with per-run resources.
- HMAC verification running first in the webhook handler.
- The chunk id scheme (changing it invalidates the index).
- The repo filter on every retrieval.
- Session and access checks on user-facing routes. User tokens stay on the backend.

## Keeping docs current

Keep these files short. Record decisions and constraints, not change logs, because git history covers that.

- Structure, storage, invariants → `architecture.md`
- Conventions → `code-standards.md`
- Scope, features, roadmap → `project-overview.md`

When a phase lands, update the roadmap in `project-overview.md`.

## Done means

The service starts (`docker compose up`, `uvicorn`, `celery worker`), the feature can be
triggered and produces the expected result, and no invariant is broken.
