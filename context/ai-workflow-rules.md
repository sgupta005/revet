# AI Workflow Rules

## Approach

Build this project incrementally using a phase-driven workflow. The context files define
what to build, how to build it, and the current state of progress. Always implement
against these specs — do not infer or invent behavior not described here. Each phase
must be fully working before the next begins; earlier phases are depended on by later
ones (indexing must precede all AI features).

## Build Order (Phases)

| Phase | Deliverable |
|-------|-------------|
| **0. Scaffold** | Repo layout; `docker-compose` (Postgres+pgvector / Redis); `.env.example`; config; `/health`; SQLModel models (`create_all`); LangSmith wired. |
| **1. GitHub App + Webhooks** | Token minting (+Redis cache); HMAC verify; webhook router; idempotency; Celery app + stub tasks enqueued. |
| **2. Indexing** | Tree-sitter chunker; embeddings; pgvector upsert (deterministic ids); incremental re-index on push; status transitions; retriever. |
| **3. AI Foundation** | `llm.py`, `tools.py`, `schemas.py`, `checkpointer.py`; LangSmith tracing on. |
| **4. Chat** | Corrective + agentic RAG graph + streaming `/chat` (validates the foundation early). |
| **5. PR Review** | Multi-agent fan-out graph → posted review + activity row. |
| **6. Issue Analysis** | Agentic-RAG graph → comment + activity row. |
| **7. Auto-PR** | Plan→generate→commit graph → PR + link comment (label-gated). |
| **8. Evals** | Golden datasets + `langsmith.evaluate` + LLM-as-judge for review & chat. |
| **9. Polish** | Celery retries/backoff + dead-letter; structured logging; Flower; README + diagram. |

Phase 2 (Indexing) must precede Phases 4–7 (all AI features depend on the vector index).
Phase 3 (AI Foundation) must precede Phases 4–7 (shared building blocks).

## Scoping Rules

- Work on one phase unit at a time; complete it end-to-end before moving on.
- Prefer small, verifiable increments over large speculative changes.
- Do not combine unrelated system boundaries in a single implementation step.
- The indexing pipeline is deliberately a plain async pipeline, not a LangGraph graph —
  do not add unnecessary graph structure to it.

## When to Split Work

Split an implementation step if it combines:

- AI graph changes and webhook/Celery changes simultaneously
- Multiple unrelated feature graphs (e.g., adding chat and PR review in one step)
- Behavior not clearly defined in the context files
- A change that cannot be verified end-to-end quickly

## Handling Missing Requirements

- Do not invent product behavior not defined in the context files or the PRD.
- If a requirement is ambiguous, resolve it in the relevant context file before implementing.
- If a requirement is missing, add it as an open question in `progress-tracker.md` before continuing.

## Protected Patterns

Do not change the following without explicit instruction:

- The Celery task → `asyncio.run(graph.ainvoke(...))` pattern (required for prefork workers).
- The `X-Hub-Signature-256` HMAC verification step — it must always run first in webhook handlers.
- Deterministic chunk id scheme (`hash(repo + path + line-span)`) — changing it invalidates the index.
- The repo-scope filter on every retrieval call — cross-repo leakage is a correctness bug.

## Keeping Docs in Sync

Update the relevant context file whenever implementation changes:

- System architecture, boundaries, or component responsibilities → `architecture.md`
- Storage model decisions → `architecture.md`
- Code conventions or standards → `code-standards.md`
- Feature scope or success criteria → `project-overview.md`
- Always update `progress-tracker.md` after completing any meaningful step.

## Before Moving to the Next Phase

1. The current phase works end-to-end within its defined scope.
2. No invariant defined in `architecture.md` was violated.
3. `progress-tracker.md` is updated to reflect the completed phase.
4. The service starts cleanly (`docker-compose up` + `uvicorn` + `celery worker`).
5. The feature can be manually triggered and produces the expected output (webhook → task → result).
