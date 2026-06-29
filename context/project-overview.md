# AI Code Review & Codebase Assistant

## Overview

A self-hosted, AI-powered GitHub assistant. Once its GitHub App is installed on a
repository, it reviews pull requests with a structured multi-perspective review,
analyzes new issues with code-aware comments, auto-generates fix PRs from labeled
issues, and lets users chat with their codebase via a semantic index. Every
codebase-aware capability is powered by Tree-sitter chunking + pgvector embeddings
orchestrated with LangGraph/LangChain. The core engine is a single backend service;
a separate Next.js frontend (`../revet_fe`) consumes it over a session-gated REST/SSE
API (added in Phase 5 — GitHub OAuth + user-facing endpoints).

## Goals

1. Build the AI engine as a set of LangGraph graphs/agents demonstrating multi-agent
   orchestration, tool-using agents, corrective RAG, structured output, durable
   execution, and evaluation.
2. Ship working features end-to-end: index a repo, review PRs, analyze issues, open
   auto-PRs, and answer codebase questions.
3. Keep the system simple and observable: one Python service, two backing stores
   (Postgres + Redis), full LLM tracing via LangSmith, idempotent background jobs.

## Core User Flow

1. User installs the GitHub App on a repository.
2. System auto-indexes the repo (Tree-sitter chunks → embeddings → pgvector).
3. When a PR is opened, the bot posts a structured multi-perspective code review.
4. When an issue is opened, the bot posts a code-aware analysis comment.
5. When an issue is labeled `auto-fix`, the bot opens a fix PR.
6. User sends natural-language questions to `/chat` and receives grounded, streamed
   answers backed by the codebase index.

## Features

### GitHub App Integration & Webhooks (F1)
- HMAC-verified (`X-Hub-Signature-256`) webhook intake
- Installation events persist `Installation` + `Repository` rows and enqueue indexing
- Webhook delivery-id dedup via Redis; enqueue + `200` in < 1 s
- App JWT (RS256) → installation token cached in Redis with TTL

### Repository Indexing — Semantic Index (F2)
- Recursive file fetch skipping lockfiles, binaries, vendored/build dirs, >100 KB files
- Tree-sitter chunking (function/class-aware) across 18 languages; whole-file fallback
- Embeddings upserted to pgvector with deterministic ids (idempotent re-index)
- `indexing_status` transitions: `NOT_STARTED → INDEXING → COMPLETED | FAILED`
- Incremental re-index on `push`: delete old points for changed paths, re-chunk

### Chat with Codebase — Corrective + Agentic RAG (F3)
- Grounded answers showing path, symbol, and line range
- Corrective RAG loop: grade documents → rewrite query if weak → retrieve again (max ~2 loops)
- Responses stream via SSE; conversation memory persisted per `thread_id` via checkpointer
- *(Phase 6)* Thread ownership: `ChatThread` row links each `thread_id` to the user + repo; title auto-set from the first message (≤80 chars)
- *(Phase 6)* `GET /repos/{owner}/{repo}/chat/threads` lists the user's threads for a repo; `GET /chat/threads/{thread_id}` returns the full message history (read back from the LangGraph checkpointer); both access-checked

### AI Issue Analysis — Agentic RAG / ReAct (F4)
- ReAct agent explores the index (search → read → follow references) on each new issue
- Posts structured comment: likely files, approach, existing patterns
- Respects custom review rules; writes `Issue` activity row

### AI Pull Request Review — Multi-agent Fan-out (F5)
- Fetches diff + changed files, retrieves related code (repo-scoped)
- Parallel `Send` fan-out to: correctness, security, code-quality, custom-rules reviewers
- Each reviewer emits `list[ReviewFinding]` (structured output); aggregate dedupes + ranks
- Posts one review (single comment or inline via GitHub Reviews API); writes `PullRequest` row

### Auto-PR Generation from Issues (F6)
- Label-gated (`auto-fix`); never auto-merged; PRs clearly marked bot-generated
- Produces strict JSON `FixPlan` (summary, approach, files with path/action/rationale)
- Generates complete file contents; creates branch `ai-fix/issue-<n>`, commits, opens PR
- Comments PR link on the issue; writes `PullRequest` row (`kind="auto-pr"`)
- Graph is checkpointed for durability; future slots: reflection loop + HITL interrupt

### Custom Review Rules (F7)
- CRUD for rules scoped to an installation (cap ~50)
- Rules fetched and injected into review, issue analysis, and auto-PR prompts

### Observability & Evaluation (F8)
- LangSmith auto-traces every graph run (fan-out, corrective-RAG loops, per-node cost)
- Eval harness (`evals/`) with golden datasets + `langsmith.evaluate` + LLM-as-judge
- Evaluators: review usefulness, retrieval relevance, auto-PR plan correctness

### User Auth & Frontend API (F9) — *added for the web frontend (`../revet_fe`)*
- **GitHub OAuth** (the GitHub App's user-to-server tokens) → lightweight GitHub-only
  identity (no passwords, no separate accounts). `POST /auth/session` exchanges the OAuth
  `code` for a user token, upserts a `User`, and creates a **Redis session**; `POST /auth/logout` ends it.
- **Dual-token model**: the **installation token** still does all repo work; the **user
  token** only resolves identity and access (`GET /user`, `GET /user/installations`).
- **Session-gated, access-checked REST**: `GET /me`, `GET /installations/{id}/repositories`
  (+ indexing status), `POST /repos/{owner}/{repo}/index`, `GET /repos/{owner}/{repo}/index-status`,
  and a now-gated `/chat`. Every installation/repo call verifies the user can access it.
- **CORS** for the frontend origin (credentialed). Full contract:
  `../revet_fe/context/github-integration.md`.

## Scope

### In Scope
- FastAPI webhook intake + `/chat` (streaming SSE) + `/health`
- *(Phase 5)* User-facing REST API + GitHub OAuth sign-in + Redis sessions for the web frontend
- Celery background jobs: `index_repo`, `review_pr`, `analyze_issue`, `auto_pr`
- LangGraph graphs for each AI feature
- Postgres + pgvector (embeddings + relational data + LangGraph checkpointer)
- Redis (Celery broker + result backend + token cache + dedup)
- GitHub App integration (JWT, installation tokens, REST API calls)
- LangSmith tracing + evaluation harness

### Out of Scope (v1)
- Passwords / email accounts / any identity provider other than GitHub (auth is the
  lightweight GitHub OAuth of F9; the web UI itself lives in the separate `../revet_fe` repo)
- Billing / subscriptions / plan limits
- Real-time notifications / inbox
- Org-wide policy, fine-grained RBAC, multi-tenant scale hardening
- Auto-merging generated PRs (never)

## Success Criteria

1. Installing the GitHub App triggers indexing and `indexing_status` reaches `COMPLETED`.
2. Opening a PR causes the bot to post one structured review covering correctness,
   security, and code quality within a reasonable time.
3. Opening an issue causes the bot to post a code-aware comment identifying likely files.
4. Labeling an issue `auto-fix` causes the bot to open a valid fix PR on a new branch.
5. Sending a question to `/chat` returns a streamed, grounded answer referencing specific
   code locations.
6. LangSmith captures a full trace for every graph run.
7. `evals/run_eval.py` produces scores for review usefulness and retrieval relevance.
