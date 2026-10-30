# Revet — Project Overview

A self-hosted AI GitHub assistant. Once its GitHub App is installed on a repo, it:

- **Indexes the repo** (Tree-sitter chunks → OpenAI embeddings → pgvector).
- **Reviews pull requests** with one structured, multi-perspective review.
- **Analyzes new issues** with a code-aware comment (likely files, approach).
- **Opens fix PRs** for issues labeled `auto-fix`.
- **Answers questions** about the codebase via a streamed chat (RAG).

Each AI feature is a LangGraph graph, not a single LLM call. This repo is the backend.
The Next.js frontend lives in `../revet_fe` and talks to it over a session-gated REST/SSE API.

## Features

| ID | Feature | Shape |
|----|---------|-------|
| F1 | GitHub App + webhooks | HMAC-verified intake, dedup, enqueue |
| F2 | Repo indexing | Plain async pipeline (not a graph), incremental on `push` |
| F3 | Chat with codebase | Corrective + agentic RAG, streamed, memory per thread |
| F4 | Issue analysis | ReAct agent over code tools |
| F5 | PR review | Parallel reviewers (correctness, security, quality, custom rules) → one review |
| F6 | Auto-PR | Locate → plan → generate files → commit → open PR (label-gated, never auto-merged) |
| F7 | Custom rules | Per-repo rules injected into review, issue and auto-PR prompts (max 50) |
| F8 | Observability + evals | LangSmith tracing; LLM-as-judge eval harness |
| F9 | Auth + frontend API | GitHub OAuth, Redis sessions, access-checked REST |

## Status and roadmap

Phases 0–9 are done: scaffold, webhooks, indexing, AI foundation, chat, auth, chat
history, PR review, issue analysis and auto-PR.

Up next:

1. **Phase 10 — Evals**: golden datasets, `langsmith.evaluate`, LLM-as-judge.
2. **Phase 11 — Custom rules CRUD**: `GET/POST /repos/{owner}/{repo}/rules`,
   `PUT/PATCH/DELETE .../rules/{rule_id}`. Access-checked, and the rule must belong to the path repo.
3. **Phase 12 — Polish**: Celery dead-letter queue, structured logging, Flower, README.
4. **Phase 13 — PR close events** (post-v1): handle `pull_request.closed` and set
   `PullRequest.state` on rows we track.
5. **Phase 14 — Manage repos from the UI** (post-v1): wrap
   `PUT/DELETE /user/installations/{id}/repositories/{repo_id}` with the user token. This only
   works for "selected repos" installations. A first install, or switching to all-repos mode,
   still needs the GitHub redirect.

## Out of scope

Passwords or any identity provider besides GitHub · billing · real-time notifications ·
org policy/RBAC · auto-merging generated PRs · production autoscaling.
