# Product Requirements Document — AI Code Review & Codebase Assistant

> **Working name:** _TBD_ · **Status:** v1 design · **Author:** Shivam Gupta

---

## 1. Overview

A self-hosted, AI-powered GitHub assistant. Once its **GitHub App** is installed on a repository, it:

- **Reviews pull requests** and posts a structured, multi-perspective review.
- **Analyzes new issues** and posts a code-aware comment pointing at likely files and an approach.
- **Auto-generates a fix PR** from an issue (plan → generate files → open PR).
- Lets a user **chat with their codebase** over a semantic index (RAG).

Every "codebase-aware" capability is powered by a semantic index of the repository (Tree-sitter
chunking + embeddings) and orchestrated with **LangGraph / LangChain**. The product's defining
characteristic is its **AI architecture**: each feature is a purpose-built graph or agent — multi-agent
fan-out for review, agentic/corrective RAG for chat and issues, plan-and-execute for auto-PR — with
durable execution, structured outputs, tracing, and an evaluation harness.

**v1 ships as a single backend service.** A web UI (dashboard, auth) is explicitly a later phase and is
out of scope here; the AI engine is fully functional without it (webhooks are signature-verified).

---

## 2. Goals & Non-Goals

### Goals
- Build the AI engine as a set of **LangGraph graphs / agents** — not single LLM calls — demonstrating
  multi-agent orchestration, tool-using agents, RAG failure-mode handling, structured output, durable
  execution, and evaluation.
- Ship working features end-to-end: index a repo, review PRs, analyze issues, open auto-PRs, and answer
  codebase questions.
- Keep the system **simple and observable**: one Python service, two backing stores (Postgres + Redis),
  full LLM tracing, idempotent and retried background jobs.

### Non-Goals (v1)
- ❌ Web frontend, user accounts, and auth (later phase).
- ❌ Billing / subscriptions / plan limits.
- ❌ Real-time notifications / inbox.
- ❌ Org-wide policy, fine-grained RBAC, multi-tenant scale hardening.
- ❌ Auto-merging generated PRs (never).

---

## 3. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Service runtime | **Python 3.13 + FastAPI + uvicorn** | Webhook intake, sync chat endpoint, health |
| Background jobs | **Celery + Redis** | Broker + result backend; indexing / review / issue / auto-PR |
| AI orchestration | **LangGraph** | `StateGraph`, `Send` fan-out, conditional edges, checkpointer, prebuilt ReAct |
| AI components | **LangChain** | `init_chat_model`, prompt templates, `with_structured_output`, `@tool`, `langchain-postgres` retriever |
| LLM | **OpenAI** via `init_chat_model("openai:<model>")` | Provider-swappable by changing one config string |
| Embeddings | **OpenAI `text-embedding-3-small`** | 1536-dim |
| Vector store | **pgvector** (Postgres extension) | Embeddings live in the same Postgres — no separate vector service |
| Relational DB + ORM | **PostgreSQL + SQLModel** | Tables via `metadata.create_all()`; **no migration tool in v1** |
| Graph durability | **LangGraph Postgres checkpointer** (`AsyncPostgresSaver`) | Same Postgres; powers chat memory + durable auto-PR |
| Code chunking | **Tree-sitter** | Function/class-aware semantic chunks |
| Source / events | **GitHub App** | App JWT → installation token; webhook events |
| Observability | **LangSmith** + eval harness | Auto-trace every graph run; LLM-as-judge evals |
| Queue visibility | **Flower** (optional) | Inspect tasks/retries |

**Backing services (docker-compose):** PostgreSQL (with `pgvector`) + Redis. That's it.

---

## 4. System Architecture

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

### 4.1 Components
- **FastAPI app** — receives GitHub webhooks (verifies `X-Hub-Signature-256`), enqueues Celery jobs,
  returns `200` fast; serves the **synchronous streaming `/chat`** endpoint and `/health`.
- **Celery worker(s)** — consume jobs and run the corresponding **LangGraph graph**; persist results
  (activity rows, indexing status) to Postgres; handle retries/backoff/dedup.
- **Postgres (+pgvector)** — single source of truth: relational tables (installations, repos, rules,
  activity), the **code-chunk embeddings** (pgvector), and the **LangGraph checkpointer** state.
- **Redis** — Celery broker + result backend; also caches GitHub installation tokens and webhook
  delivery-id dedup keys.

### 4.2 Queues (Celery tasks)
| Task | Trigger | Action |
|---|---|---|
| `index_repo` | App installed / repo added / `push` | Index (or incrementally re-index) the repo into pgvector |
| `review_pr` | `pull_request` opened/synchronized | Run the multi-agent review graph; post review |
| `analyze_issue` | `issues` opened | Run the agentic-RAG issue graph; post comment |
| `auto_pr` | issue labeled `auto-fix` | Run the plan→generate→commit graph; open a PR |

**Chat is not queued** — it is synchronous request/response (latency matters; user is waiting).

### 4.3 Core flows
- **Indexing:** install/push → `index_repo` → set `INDEXING` → list files → Tree-sitter chunk → embed →
  upsert pgvector (deterministic ids) → set `COMPLETED`/`FAILED`.
- **PR review:** PR event → verify + enqueue `review_pr` → graph fetches diff + related code → parallel
  reviewers → aggregate → post one review → write activity row.
- **Issue analysis:** issue event → `analyze_issue` → ReAct agent explores the index → post comment →
  write activity row.
- **Auto-PR:** labeled issue → `auto_pr` → locate → plan → generate files → branch/commit → open PR →
  comment link.
- **Chat:** browser → `/chat` → corrective+agentic RAG graph (streamed) → grounded answer.

---

## 5. AI Orchestration Foundation (shared building blocks)

Built first; every feature graph depends on it.

- **`ai/llm.py`** — `chat = init_chat_model(settings.llm_model)` (provider is one config string);
  `embeddings = OpenAIEmbeddings("text-embedding-3-small")`.
- **`ai/vectorstore.py` + `ai/retriever.py`** — `langchain_postgres.PGVector` over the app's Postgres;
  `retriever_for(repo)` applies a metadata filter `{"repo": repo}` so search is always repo-scoped.
- **`ai/tools.py`** — LangChain `@tool`s that make features agentic: `retrieve_code(query)` (semantic
  search), `read_file(path)` (full file via GitHub Contents API), `grep_symbol(name)`,
  `list_directory(path)`, `get_file_tree()`.
- **`ai/schemas.py`** — Pydantic models for `with_structured_output`: `ReviewFinding {file, line,
  severity, category, comment, confidence}`, `FixPlan {summary, approach, files:[{path, action,
  rationale}]}`, `RelevanceGrade {relevant: bool}`.
- **`ai/checkpointer.py`** — `AsyncPostgresSaver`; graphs `.compile(checkpointer=...)` to get
  conversation memory (chat, keyed by `thread_id`) and durable/resumable long jobs (auto-PR).

---

## 6. Data Model

### Relational (SQLModel — tables auto-created at startup, no migration tool in v1)
```python
class IndexingStatus(str, Enum): NOT_STARTED; INDEXING; COMPLETED; FAILED

class Installation(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    installation_id: int = Field(unique=True, index=True)   # GitHub installation id
    account_login: str
    created_at: datetime

class Repository(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    github_id: int = Field(unique=True, index=True)
    name: str
    full_name: str
    installation_id: int = Field(foreign_key="installation.id")
    indexing_status: IndexingStatus = IndexingStatus.NOT_STARTED
    indexed_at: datetime | None
    created_at: datetime

class Rule(SQLModel, table=True):                 # custom review guideline, scoped to an installation
    id: int | None = Field(primary_key=True)
    content: str
    installation_id: int = Field(foreign_key="installation.id", index=True)
    created_at: datetime; updated_at: datetime

class PullRequest(SQLModel, table=True):          # activity record (review or auto-pr)
    id: int | None = Field(primary_key=True)
    github_id: int; number: int; title: str
    kind: str = "review"                          # "review" | "auto-pr"
    repository_id: int = Field(foreign_key="repository.id", index=True)
    created_at: datetime

class Issue(SQLModel, table=True):                # activity record
    id: int | None = Field(primary_key=True)
    github_id: int; number: int; title: str
    repository_id: int = Field(foreign_key="repository.id", index=True)
    created_at: datetime
```

### Code-chunk embeddings (pgvector, via `langchain-postgres`)
Each chunk = one row: `embedding vector(1536)` + metadata `{repo, path, name, chunk_type, language,
start_line, end_line}` and the chunk text as page content. **Deterministic id = hash(repo + path +
line-span)** so re-indexing upserts instead of duplicating. Retrieval filters on `repo`.

---

## 7. Functional Requirements

> Each: **user story · acceptance criteria (AC) · implementation notes.**

### F1 — GitHub App Integration & Webhooks
**Story:** As a user, I install the GitHub App on a repo so the bot can read it and act on it.
**AC:**
- Installation events persist an `Installation` + its `Repository` rows and enqueue `index_repo`.
- Every webhook is **HMAC-verified** (`X-Hub-Signature-256`) before processing; invalid → `401`.
- The endpoint verifies + enqueues + returns `200` in well under a second.
**Notes:** App JWT (RS256) → installation token, cached in Redis with TTL; token minting lives in one
place. Idempotency via `X-GitHub-Delivery` as the Celery `task_id` (or a Redis dedup set).

### F2 — Repository Indexing (semantic index)
**Story:** As the system, I index a connected repo so all AI features are codebase-aware.
**AC:**
- Files fetched recursively, skipping lockfiles, binaries, vendored/build dirs, and files > 100 KB.
- Files chunked by **Tree-sitter** into functions/classes (whole-file fallback) across the common
  languages (Python, JS/TS/TSX, Go, Java, Rust, Ruby, PHP, C/C++, C#, HTML, CSS, JSON, YAML, TOML, MD,
  Bash, SQL, Dockerfile).
- Each chunk embedded and upserted into pgvector with a deterministic id (idempotent re-index).
- `Repository.indexing_status` transitions `NOT_STARTED → INDEXING → COMPLETED` (or `FAILED`).
- On `push`, **only changed files** are re-indexed (delete old points for those paths, re-chunk).
**Notes:** Plain async pipeline as a Celery task — **deliberately not a graph** (no reasoning step;
a graph here would be over-engineering). Batch embeddings to control cost/latency.

### F3 — Chat with Codebase — Agentic + Corrective RAG (streaming, memory)
**Story:** As a user, I ask natural-language questions about my repo and get grounded answers.
**AC:**
- Answers are grounded in retrieved code (path, symbol, line range shown to the LLM).
- If retrieval is weak, the system reformulates the query and retrieves again before answering.
- Responses **stream**; conversation context persists per chat session.
**Notes (`ai/graphs/chat.py`):**
```
retrieve ─▶ grade_documents ─┬─(relevant)─▶ generate ─▶ END
                             └─(weak)─────▶ rewrite_query ─▶ retrieve   (bounded ~2 loops)
```
`grade_documents` uses `with_structured_output(RelevanceGrade)` (corrective RAG). `generate` is
tool-using (can `read_file` for full context, not just chunks). Streaming via `astream_events` /
`stream_mode="messages"` → SSE from the **synchronous** `/chat` endpoint. Memory via checkpointer
`thread_id`.

### F4 — AI Issue Analysis — Agentic RAG (ReAct)
**Story:** As a maintainer, when an issue is opened I get a helpful, code-aware comment.
**AC:**
- The bot identifies likely files, suggests an approach, and references existing patterns.
- Custom rules (F7) are respected; an `Issue` activity row is written.
**Notes (`ai/graphs/issue_analysis.py`):** a `create_react_agent` (or custom ToolNode + conditional
edge) given the code tools; it explores the repo (search → read candidates → follow references) until it
can emit a structured suggestion, then posts the comment. Celery task `analyze_issue`.

### F5 — AI Pull Request Review — Multi-agent parallel fan-out
**Story:** As a maintainer, when a PR is opened I get one structured, multi-perspective review.
**AC:**
- The bot fetches the diff + changed files, retrieves related code (repo-scoped), and posts **one**
  review covering correctness, security, code quality, and custom-rule violations.
- Custom rules are injected when present; a `PullRequest` row (`kind="review"`) is written.
**Notes (`ai/graphs/pr_review.py`):**
```
prepare ─▶ retrieve_context ─▶ [Send fan-out] ─▶ correctness_reviewer ┐
                                                 security_reviewer     ├▶ aggregate ─▶ format_post
                                                 quality_reviewer      │  (merge/dedupe/rank)
                                                 custom_rules_reviewer ┘
```
Each reviewer is a focused agent emitting `list[ReviewFinding]` (structured output), collected via a
reducer (`Annotated[list, operator.add]`). `aggregate` dedupes + ranks by severity/confidence into one
review (single comment, or inline via the GitHub Reviews API). Focused per-concern prompts hallucinate
far less than one mega-prompt. Celery task `review_pr`.

### F6 — Auto-PR Generation from Issues
**Story:** As a maintainer, the bot can open a PR that attempts to fix an issue.
**AC:**
- Produces a strict JSON **`FixPlan`** (summary, approach, `files[]` with path/action/rationale).
- Generates **complete** new file contents (no diffs/placeholders).
- Creates branch `ai-fix/issue-<n>`, commits create/update/delete changes, opens a PR (`Closes #n`,
  body with summary/changes/approach), and comments the PR link on the issue.
- **Trigger is label-gated** (`auto-fix`) to avoid noise; PRs are clearly bot-generated and never
  auto-merged. A `PullRequest` row (`kind="auto-pr"`) is written.
**Notes (`ai/graphs/auto_pr.py`):**
```
locate (agentic retrieval) ─▶ plan (FixPlan) ─▶ [fan-out per file] generate_file
   ─▶ commit (branch + create/update/delete) ─▶ open_pr (+ link comment on issue)
```
Low temperature for plan/generation; checkpointed for durability. **Future enhancements** (designed to
slot in without restructuring): a reflection/self-critique loop (critic node + `py_compile`/lint tool,
loop back on failure) and a human-in-the-loop `interrupt()` approval gate before `commit`.

### F7 — Custom Review Rules
**Story:** As a user, I define guidelines the bot enforces across reviews, issue help, and auto-PRs.
**AC:** CRUD for rules scoped to an installation; rules fetched and injected into the relevant prompts;
a generous fixed cap (e.g. 50) bounds prompt size.
**Notes:** Stored in the `Rule` table; loaded in `prepare`/agent setup and formatted into prompts.

### F8 — Observability & Evaluation (the maturity differentiator)
**Story:** As the builder, I can trace every AI run and measure output quality.
**AC:**
- With `LANGSMITH_TRACING=true`, every graph run auto-traces (parallel reviewer fan-out, the
  corrective-RAG retrieve→grade→rewrite loop, per-node tokens/cost/latency).
- An **eval harness** scores outputs on small golden datasets.
**Notes (`evals/`):** golden sets (PRs with known issues; issues with known target files; chat Q/A
pairs); `run_eval.py` uses `langsmith.evaluate` with **LLM-as-judge** evaluators for review usefulness,
retrieval relevance, and auto-PR plan correctness.

---

## 8. Non-Functional Requirements

- **Idempotency:** dedupe webhook redeliveries by delivery id; re-indexing safe via deterministic ids.
- **Reliability:** Celery `autoretry_for` transient GitHub/OpenAI/DB errors with exponential backoff
  (3–5 attempts); failures land in a dead-letter queue.
- **Fast webhook ACK:** verify + enqueue + `200` in < ~1s; all heavy work in the queue.
- **Sync↔async bridge:** Celery tasks run `asyncio.run(graph.ainvoke(...))`; async clients created
  inside the task to avoid cross-loop reuse (prefork workers).
- **Secrets:** GitHub private key, OpenAI key, webhook secret, DB/Redis creds, LangSmith key via env
  only; never log tokens or full file contents.
- **Observability:** structured per-task logs (task id, repo, duration, outcome) + LangSmith traces; a
  `/health` endpoint; optional Flower for queue visibility.
- **Local dev:** `docker-compose` for Postgres (+pgvector) + Redis; `.env.example`.

---

## 9. Configuration (env vars)

`DATABASE_URL` (Postgres w/ pgvector) · `REDIS_URL` · `OPENAI_API_KEY` · `LLM_MODEL`
(e.g. `openai:gpt-4o`) · `EMBEDDING_MODEL` (default `text-embedding-3-small`) · `GITHUB_APP_ID` ·
`GITHUB_APP_PRIVATE_KEY` · `GITHUB_WEBHOOK_SECRET` · `LANGSMITH_TRACING` · `LANGSMITH_API_KEY` ·
`LANGSMITH_PROJECT` · `ENVIRONMENT` · `LOG_LEVEL`.

---

## 10. Milestones / Build Order

| Phase | Deliverable |
|---|---|
| **0. Scaffold** | Repo layout; `docker-compose` (Postgres+pgvector / Redis); `.env.example`; config; `/health`; SQLModel models (`create_all`); LangSmith wired. |
| **1. GitHub App + webhooks** | Token minting (+Redis cache); HMAC verify; webhook router; idempotency; Celery app + stub tasks enqueued. |
| **2. Indexing** | Tree-sitter chunker; embeddings; pgvector upsert (deterministic ids); incremental re-index on push; status transitions; retriever. |
| **3. AI foundation** | `llm.py`, `tools.py`, `schemas.py`, `checkpointer.py`; LangSmith tracing on. |
| **4. Chat** | Corrective+agentic RAG graph + streaming `/chat` (validates the foundation early). |
| **5. PR review** | Multi-agent fan-out graph → posted review + activity row. |
| **6. Issue analysis** | Agentic-RAG graph → comment + activity row. |
| **7. Auto-PR** | Plan→generate→commit graph → PR + link comment (label-gated). |
| **8. Evals** | Golden datasets + `langsmith.evaluate` + LLM-as-judge for review & chat. |
| **9. Polish** | Celery retries/backoff + dead-letter; structured logging; Flower; README + diagram. |

Indexing (Phase 2) must precede review/chat/issue/auto-PR (they depend on the vector index).

---

## 11. Risks & Open Questions

- **LLM cost/latency:** indexing embeds every chunk; auto-PR regenerates whole files; review fans out to
  several agents. Mitigate: cap file sizes, batch embeddings, modest retrieval `limit`, small/cheaper
  models for graders/reviewers.
- **Auto-PR quality:** generated fixes may be wrong — keep label-gated, mark PRs bot-generated, never
  auto-merge. The future reflection/HITL loop is the main quality lever.
- **Agent loops:** bound all corrective/ReAct loops (max iterations) to avoid runaway tool calls/cost.
- **Open Q (LLM provider):** OpenAI is the v1 default (and required for embeddings); the chat model is
  swappable via `init_chat_model`.
- **Open Q (migrations):** v1 uses `create_all()`; adopt a migration tool only if the schema starts
  churning.

---

## 12. Out of Scope (recap)

Web frontend · user accounts & auth · billing/subscriptions · usage limits · real-time notifications ·
org policy/RBAC · auto-merge · production autoscaling. These can be layered on later but are **not** part
of v1.
