# Code Standards

## General

- Keep modules small and single-purpose. Don't mix concerns, for example the webhook router and the user API.
- Fix root causes. Don't add workarounds or compatibility shims.
- Don't add features, abstractions or error handling the current task doesn't need.
- Validate only at boundaries (webhook payloads, GitHub responses, request bodies).

## Python

- Python 3.13, async throughout. Annotate every signature and avoid `Any`.
- Every function gets a short docstring: one sentence, plus any non-obvious invariant.
- Add comments only for a non-obvious *why*.
- Use Pydantic v2 for boundary data and LLM output. Use dataclasses for internal values. Use SQLModel for tables.
- Keep prompts in `ai/prompts.py` and tunable numbers in `ai/constants.py`.

## FastAPI

- Webhook handlers verify, then enqueue, then return. `/chat` is the only route that runs a graph.
- Use `Depends(get_current_user)` for auth. Call `verify_installation_access` first in every repo/installation handler.
- CORS is configured once in `app/main.py`.

## Celery

- Each task is `asyncio.run(run_x(...))` with `autoretry_for` and `retry_backoff` (3 retries).
- Build per-run resources inside the task. Don't reuse a cached singleton (see architecture invariant 3).
- Use a fresh `thread_id` per run so reducer channels never carry over.

## LangGraph / LangChain

- Each feature is a `StateGraph`. Indexing is the exception: it's a plain pipeline.
- Use `Send` for fan-out and `Annotated[list, operator.add]` to collect results. Write
  aggregated results to a separate channel, because writing back to a reducer appends.
- Use `with_structured_output` for any output that code consumes.
- In Celery graphs, use `make_chat_model()` (a fresh model per call), not `get_chat_model()`.
- Use cheap models (`gpt-4o-mini`) for graders and reviewers, and the default model for generation.
- When a tool loop hits its cap, drop the tools so the turn ends with text.
- Stream chat with `stream_mode="messages"`. Never buffer the whole response.

## Indexing

- Skip lockfiles, `.min.` files, binaries, vendored/build dirs and files over 100 KB.
- Use the standard `tree_sitter` with one `tree-sitter-<lang>` package per language, not the
  bundled language pack (its binding breaks on newer Python).
- Markup and data files, and files with no definitions, become whole-file chunks. Oversized chunks are split by lines.
- Embed `embedding_text()`, which is a `File:`/symbol header plus the fenced code, never raw code. Batch the embedding calls.

## Observability

- Rely on LangSmith auto-tracing. Don't add manual trace wrappers.
- Log task id, repo, duration and outcome. Never log tokens, secrets or full file contents.
