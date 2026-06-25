# Code Standards

## General

- One Python service; keep modules small and single-purpose.
- Fix root causes; do not layer workarounds or add unnecessary compatibility shims.
- Do not mix unrelated concerns in one module or route handler.
- No comments explaining what the code does — only add one when the WHY is non-obvious (hidden constraint, subtle invariant, bug workaround).
- No features, abstractions, or error handling beyond what the current task requires.
- Validate only at system boundaries (webhook payloads, GitHub API responses, user chat input); trust internal code and LangChain/LangGraph guarantees.

## Python

- Python 3.13; use `asyncio` throughout the AI layer.
- Strict typing — annotate all function signatures; avoid `Any`.
- Every function must have a docstring. Keep it short: one sentence stating what the function does, plus a note on any non-obvious invariant or design decision (e.g. why an id is deterministic, what gets flushed and when).
- Use Pydantic v2 models for all structured data crossing system boundaries (webhook payloads, LLM outputs via `with_structured_output`, API responses).
- SQLModel for ORM models; schema created via `metadata.create_all()` at startup.
- Prefer dataclasses or named tuples for internal-only value objects; reserve SQLModel/Pydantic for boundary types.

## FastAPI

- Route handlers verify → enqueue → return `200`; no heavy work in the request path.
- The only exception is `/chat`, which runs the RAG graph synchronously and streams via SSE.
- Use `Depends()` for shared resources (DB session, settings); do not import globals in handlers.
- `/health` returns a minimal response; no AI calls.

## Celery

- Each task calls `asyncio.run(graph.ainvoke(...))`.
- Create async clients (DB, HTTP) inside the task function — never reuse across tasks (prefork workers).
- Use `autoretry_for` with exponential backoff (3–5 attempts) for transient GitHub/OpenAI/DB errors.
- Use `X-GitHub-Delivery` as the Celery `task_id` (or Redis dedup set) for idempotency.
- Failed tasks after all retries go to a dead-letter queue.

## LangGraph / LangChain

- Every feature is a `StateGraph` (or `create_react_agent`) — not a bare LLM call chain.
- Use `Send` for parallel fan-out (PR review reviewers, auto-PR file generation).
- Use `with_structured_output(Schema)` for all LLM outputs that need to be machine-consumed.
- Use `Annotated[list, operator.add]` reducers for collecting parallel node outputs.
- Bound all corrective/ReAct loops with a max-iteration guard (no runaway tool calls).
- All graphs compile with `checkpointer=checkpointer` except indexing (which is a plain async pipeline).
- Stream chat via `astream_events` / `stream_mode="messages"` — never buffer the full response.

## AI Tools (`ai/tools.py`)

- All tools are LangChain `@tool` decorated functions.
- `retrieve_code(query)` — semantic search via the repo-scoped retriever (always passes `{"repo": repo}` filter).
- `read_file(path)` — fetches full file via GitHub Contents API using the installation token.
- `grep_symbol(name)`, `list_directory(path)`, `get_file_tree()` — navigation tools.
- Tools must not leak secrets; never log full file contents.

## AI Schemas (`ai/schemas.py`)

```python
class ReviewFinding(BaseModel):
    file: str; line: int; severity: str; category: str
    comment: str; confidence: float

class FixPlan(BaseModel):
    summary: str; approach: str
    files: list[FixFile]  # {path, action, rationale}

class RelevanceGrade(BaseModel):
    relevant: bool
```

## Embeddings and Vector Store

- Model: `text-embedding-3-small` (1536-dim) via `OpenAIEmbeddings`.
- Embed a context-enriched string, not raw code: a `File: <path>` + `<type>: <name>` header followed by the fenced snippet (`embedding_text()`). The same string is stored as the document and returned to the LLM, so retrieval and grounding both carry path/symbol context.
- Batch embedding calls to control cost/latency; do not embed one chunk at a time.
- Deterministic chunk id = `hash(repo + path + line-span)` — upsert, never insert blindly.
- Metadata per chunk: `{repo, path, name, chunk_type, language, start_line, end_line}`.
- Every retrieval call passes `filter={"repo": repo_full_name}`.

## GitHub Integration

- Token minting lives in one place (`app/github/auth.py`); cached in Redis with TTL.
- HMAC verification (`X-Hub-Signature-256`) happens before any other webhook processing.
- Never log installation tokens, private keys, or webhook secrets.
- All GitHub REST calls use the scoped installation token.

## Indexing Pipeline

- Implemented as a plain async pipeline inside the Celery task — deliberately not a LangGraph graph (no reasoning step; a graph here would be over-engineering). `index_repo` calls `asyncio.run(run_index(...))`; the pipeline builds its own DB engine, embeddings, and PGVector per run (prefork-safe, invariant #3).
- Skip: lockfiles, minified bundles (`.min.` in filename), binaries (extension allow-list + null-byte check), vendored/build dirs, files > 100 KB.
- Languages: Python, JS/TS/TSX, Go, Java, Rust, Ruby, PHP, C/C++, C#, HTML, CSS, JSON, YAML, TOML, MD, Bash, SQL, Dockerfile.
- Use the standard `tree_sitter` package (`Parser`/`Language`/`node.type`) with one `tree-sitter-<lang>` grammar package per language — not a bundled multi-language pack (its binding diverged from py-tree-sitter on Python 3.14).
- Structural (function/class-aware) chunking runs for the programming languages with a loaded grammar; data/markup languages (JSON, YAML, TOML, MD, HTML, CSS) and any file whose grammar yields no definitions fall back to whole-file chunks. Oversized chunks are split into line windows under the embedding token limit.

## Observability

- Use `LANGSMITH_TRACING=true` to auto-trace all graph runs — do not add manual trace wrappers.
- Emit structured per-task logs: task id, repo, duration, outcome.
- Never log tokens, full file contents, or secrets.

## File Organization

- `app/` — FastAPI app, routes, startup, settings
- `app/workers/` — Celery app + task implementations
- `app/github/` — GitHub auth (token minting), HMAC, REST helpers
- `app/db/` — SQLModel models, engine, session factory
- `ai/` — AI foundation (llm, vectorstore, retriever, tools, schemas, checkpointer)
- `ai/graphs/` — One file per feature graph
- `evals/` — Golden datasets and eval runner
- `docker-compose.yml` + `.env.example` — local dev setup
