# Progress Tracker

Update this file after every meaningful implementation change.

## Current Phase

Phase 10 — Complete

## Current Goal

Phase 11 — Custom Rules CRUD API (per-repo, access-checked REST under
`/repos/{owner}/{repo}/rules`), consumed by the frontend Rules tool.

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

- **Phase 4 — Chat** (2026-06-27)
  - `ai/graphs/chat.py` — corrective + agentic RAG `StateGraph` matching the PRD shape: `retrieve → grade_documents → (relevant ▸ generate | weak ▸ rewrite_query → retrieve)`, then `generate → (tool_calls ▸ tools → generate | END)`. `grade_documents` uses `with_structured_output(RelevanceGrade)`; `generate` binds `[retrieve_code, read_file]` so it can read full files, not just chunks. State carries per-thread `messages` (memory), plus `query`/`rewrites` for the corrective loop.
  - **Two bounded loops** (invariant #10): corrective rewrite loop capped at `MAX_REWRITES=2` (on cap, answers with what it has); agentic generate tool loop capped at `MAX_TOOL_ROUNDS=3` (on cap, `generate` drops tools so the turn always ends with a streamable text answer — never a dangling tool call).
  - `ai/constants.py` — `MAX_REWRITES`, `MAX_TOOL_ROUNDS`, `GRADER_MODEL="openai:gpt-4o-mini"` (cheap model for grade/rewrite; generation uses default `settings.llm_model` = gpt-4o).
  - `ai/retriever.py` — extracted `format_doc(doc)` (path:line header + page content); `ai/tools.py` now reuses it (removed its private duplicate).
  - `ai/checkpointer.py` — added `setup_checkpointer()` (enters the saver context, runs idempotent `setup()` once at startup).
  - `app/chat.py` — `POST /chat`: resolves repo → GitHub installation id (404 if app not installed) before streaming, builds `config.configurable` (`thread_id`, `repo`, `installation_id`), resets `query`/`rewrites` per turn so a checkpointed prior turn never leaks its rewritten query, then streams `graph.astream(..., stream_mode="messages")` filtered to the `generate` node's text chunks as SSE (`data: {"delta": ...}` JSON frames, terminal `{"done": true}`). Graph builder is module-level; each request compiles it against a per-request `checkpointer()` (own Postgres connection — safe under concurrent `/chat`, one event loop).
  - `app/main.py` — included `chat_router`; lifespan now runs `setup_checkpointer()` after `create_db()`.
  - Verified: all modules import; graph compiles and wiring matches the spec (edges + conditional routes); `_route_after_grade` returns rewrite when weak & under cap, generate at cap / when relevant. End-to-end `ainvoke` with fake retriever + fake model exercised the relevant path (0 rewrites → answer), the weak path (exactly 2 rewrites → answer), and empty-docs (graded weak, bounded, then answer). SSE encoder JSON-escapes newlines so multi-line tokens stay one frame. (Live OpenAI/Postgres run deferred — same as prior phases.)

- **Phase 5 — Auth & Frontend API** (2026-06-27)
  - `app/config.py` — added `github_oauth_client_id`, `github_oauth_client_secret`
    (backend-only, never exposed), `frontend_origin` (CORS), `session_ttl`; documented in
    `env.template`.
  - `app/db/models.py` — `User` table (durable identity: `github_id` unique, `login`,
    `avatar_url`); user/refresh tokens deliberately **not** stored here (they live only in
    the Redis session, invariant #12).
  - `app/github/oauth.py` — user-to-server helpers (httpx): `exchange_code` /
    `refresh_user_token` (POST `github.com/login/oauth/access_token`, secret from env),
    `get_authenticated_user` (`GET /user`), `list_user_installations`
    (`GET /user/installations`), `list_installation_repositories`
    (`GET /user/installations/{id}/repositories`, paginated). Boundary shapes are Pydantic
    (`OAuthTokens`, `GitHubIdentity`, `GitHubInstallation`, `GitHubRepo`); `OAuthError` for
    a GitHub `error` response. `refresh_token`/`expires_in` default empty so it also works
    in the App's non-expiring-token mode.
  - `app/auth/sessions.py` — Redis session store: `create_session` (opaque
    `secrets.token_urlsafe` token → `{user_id, user_token, refresh_token}`, TTL
    `settings.session_ttl`), `get_session`, `update_session_tokens` (persist a refresh),
    `delete_session`. Tokens never leave the backend.
  - `app/auth/dependencies.py` — `get_current_user` (cookie `session` **or**
    `Authorization: Bearer` → Redis → `User`; `401` if missing/invalid) returning an
    `AuthedUser`; `user_installations` (cached in Redis, `USER_CACHE_TTL=300`);
    `verify_installation_access` (`403` unless the GitHub installation id is in the user's
    list — invariant #13); `call_with_refresh` (runs a user-token call, refreshes the token
    once on a `401` and persists it, else surfaces the `401` as a re-login).
  - `app/api.py` — user-facing router (separate from the webhook router): `POST /auth/session`
    (exchange → upsert `User` → create session → `{session_token, user}`; only the opaque
    token reaches the browser), `POST /auth/logout`, `GET /me`,
    `GET /installations/{installation_id}/repositories` (access-checked; the user's live repos
    joined with stored `Repository.indexing_status`, `NOT_STARTED` when unindexed; `?refresh=1`
    bypasses the Redis repo cache), `POST /repos/{owner}/{repo}/index` (access-checked;
    enqueues the existing `index_repo`), `GET /repos/{owner}/{repo}/index-status`
    (access-checked; `indexing_status` + `chunk_count`).
  - `ai/vectorstore.py` — added `count_chunks(store, repo)` (repo-scoped `count(*)` over
    `langchain_pg_embedding`) backing index-status.
  - `app/chat.py` — `/chat` now depends on `get_current_user` and runs
    `verify_installation_access` on the resolved installation before streaming
    (session-gated + access-checked, invariant #13).
  - `app/main.py` — `CORSMiddleware` (allow `FRONTEND_ORIGIN`, credentials on, configured
    once) + `include_router(api_router)`.
  - Verified by import (all modules load, 8 user-facing routes register) and with mocked
    GitHub: session create/get/update/delete; access check allows owned installation,
    caches the installations list (one GitHub call), `403`s a foreign one; `call_with_refresh`
    refreshes once on a `401` and retries with the new token; full `TestClient` flow —
    gated route `401` without a session, `POST /auth/session` returns `{session_token, user}`
    with **no user/refresh token in the body**, `GET /me` lists installations, `POST /auth/logout`
    → subsequent `/me` `401`. (Live GitHub OAuth/Postgres run deferred — App needs OAuth
    enabled + a registered client secret, same as prior phases.)
  - Note: appended placeholder `GITHUB_OAUTH_CLIENT_ID`/`_SECRET`/`FRONTEND_ORIGIN`/`SESSION_TTL`
    to the local `.env` so the service boots; real values must be filled before live OAuth.

- **Phase 6 — Chat History** (2026-06-29)
  - `app/db/models.py` — `ChatThread` table (`thread_id` unique string, `user_id FK→User`,
    `repo` full-name, `title`, `created_at`, `updated_at`); schema created via `create_all()`
    at startup as with other tables.
  - `ai/checkpointer.py` — `get_thread_messages(thread_id)` reads the LangGraph checkpoint
    for a thread and returns `[{role, content}]`; filters to `HumanMessage`/`AIMessage` only,
    skips tool-call chunks with non-string content. Returns `[]` when no checkpoint exists.
  - `app/chat.py` — `POST /chat` now upserts a `ChatThread` row before streaming: new threads
    (no `thread_id` in request) create a row with `title = message[:80]`; existing threads
    verify `user_id == authed.user.id` (403 on mismatch, invariant #14) and touch `updated_at`
    for recency ordering. Commit happens before the `StreamingResponse` is returned.
  - `app/api.py` — two new access-checked endpoints:
    - `GET /repos/{owner}/{repo}/chat/threads` — lists the user's `ChatThread` rows for a repo
      ordered by `updated_at DESC`; access-checked via `_authorize_repo`.
    - `GET /chat/threads/{thread_id}` — verifies ownership, reads messages from the LangGraph
      checkpointer via `get_thread_messages`, returns `[{role, content}]`.
  - Response models added: `ChatThreadOut`, `MessageOut`.
  - Verified: all modules import cleanly; 2 new routes register.
- **Phase 7 — PR Review** (2026-06-27)
  - `ai/schemas.py` — added `ReviewFindings` (wraps `list[ReviewFinding]`) so a reviewer
    can emit a list via `with_structured_output` (structured output needs one top-level model).
  - `ai/constants.py` — PR-review knobs: `REVIEWER_MODEL=openai:gpt-4o-mini` (cheap per
    reviewer), `REVIEW_PERSPECTIVES=(correctness, security, quality, custom-rules)`,
    `MAX_DIFF_CHARS`, `REVIEW_CONTEXT_K`, `REVIEW_QUERY_CHARS`, `MIN_FINDING_CONFIDENCE`,
    `MAX_FINDINGS`.
  - `ai/prompts.py` — `PR_REVIEW_SYSTEM` (per-perspective, `{perspective}`/`{focus}`),
    `PR_REVIEW_PERSPECTIVE_FOCUS` map, `PR_REVIEW_HUMAN` (title/body/diff/context),
    `PR_REVIEW_RULES_BLOCK` (appended only for the custom-rules reviewer).
  - `app/github/pulls.py` — new PR REST helpers (separate concern from `files.py`):
    `fetch_pull_request` (metadata + paginated changed files, capped at `MAX_FILE_PAGES`),
    `post_review` (one review via the Reviews API, `event="COMMENT"` — never approves/
    requests-changes, no per-line diff-position mapping). Boundary shapes `PRFile`/
    `PullRequestData` are frozen dataclasses.
  - `ai/graphs/pr_review.py` — multi-agent fan-out `StateGraph` matching the PRD shape:
    `prepare → retrieve_context → [Send fan-out] review ×N → aggregate → format_post`.
    `prepare` fetches the PR (title/body/diff/changed files) + loads `repo_id` and the
    installation's custom rules. `retrieve_context` does a repo-scoped similarity search
    (invariant #6) for related code. `_fan_out` emits one `Send("review", …)` per
    perspective (custom-rules only when rules exist). `review` builds a **fresh** chat
    model per call (`make_chat_model`, never the cached singleton) and emits
    `ReviewFindings` via `with_structured_output`. `findings` uses an `operator.add`
    reducer to merge the parallel reviewers; `aggregate` **deterministically** dedupes
    (file+line+comment, keeping the highest-confidence dup), drops < `MIN_FINDING_CONFIDENCE`,
    ranks by severity then confidence, and caps at `MAX_FINDINGS` into a plain `ranked`
    channel. `format_post` renders one severity-grouped markdown review (`path:line`
    citations), posts it, and upserts the `PullRequest` activity row (kind=review, upsert on
    repo+number+kind so a `synchronize` re-review never duplicates).
  - `run_pr_review` (Celery entrypoint) builds a per-run DB engine + async PGVector store
    (prefork-safe, invariant #3), injects them + repo/installation/pr via
    `config["configurable"]`, compiles the module-level graph against a per-run
    `checkpointer()`, runs one review with a fresh `thread_id` (uuid) so the `findings`
    reducer never carries over between runs, and disposes both resources in `finally`.
  - `app/workers/tasks.py` — `review_pr` now runs `asyncio.run(run_pr_review(...))` with
    `autoretry_for`/`retry_backoff` (3 retries), matching `index_repo`.
  - Verified: all modules import; graph compiles (nodes prepare/retrieve_context/review/
    aggregate/format_post). End-to-end `ainvoke` with fakes (mocked GitHub + DB helpers +
    fake structured model + fake store) — 4 perspectives fan out with rules / 3 without;
    `operator.add` merges 6 raw findings; aggregate dedupes the duplicated finding (keeps
    0.95 over 0.6), drops the 0.1-confidence one, ranks critical→low, caps at `MAX_FINDINGS`;
    `format_post` posts one grouped markdown review and upserts the `PullRequest` row;
    empty-findings renders "No issues found"; `_build_diff` truncates oversized PRs.
    Confirmed the checkpointer persists only `thread_id`/`checkpoint_ns`/`checkpoint_id`
    + serializable state channels, so the injected `engine`/`store` objects in
    `configurable` are never serialized (same mechanism the Phase 4 chat graph relies on).
    (Live OpenAI/GitHub/Postgres run deferred — same as prior phases.)

- **Phase 10 — Evals** (2026-07-02)
  - `evals/` package (PRD §F8): a small LLM-as-judge harness over golden datasets, run
    with `langsmith.evaluate`.
  - `evals/datasets.py` — small **illustrative** golden sets (chat Q/A; issues with known
    target files; PRs with known issues; auto-fixable issues). Point `EVAL_REPO` /
    `EVAL_INSTALLATION_ID` at an already-indexed repo and fill in real numbers/answers.
  - `evals/targets.py` — runs each graph on an example. **Review / issue / auto-PR targets
    reuse the same graph builders** but compile them with `interrupt_before` at the
    posting/commit node (+ `no_fix` for auto-PR) and an in-memory checkpointer, so **evals
    never post/commit** — they run to the decision and read the accumulated state (findings /
    analysis / FixPlan). Each injects a per-run engine + async store (invariant #3) and
    disposes them. Chat runs end-to-end (no side effects); its singleton caches are cleared
    per run so each `asyncio.run` rebinds its own async clients.
  - `evals/judges.py` — LLM-as-judge evaluators (`retrieval_relevance`, `review_usefulness`,
    `plan_correctness`) using the modern langsmith `(inputs, outputs, reference_outputs)`
    signature; a cheap judge (`GRADER_MODEL`, temp 0) emits a structured `JudgeVerdict`
    (0–1 score + reasoning).
  - `evals/run_eval.py` — CLI (`python -m evals.run_eval {chat|review|issue|auto_pr}`) wiring
    each suite's target + dataset + evaluators into `langsmith.evaluate` (max_concurrency=1).
    `evals/README.md` documents required env + how to run.
  - Verified by import: all eval modules load; the three judge evaluators return the
    `{key, score, comment}` shape (fake judge); all side-effecting graphs compile with the
    `interrupt_before` nodes the targets use. Live scoring deferred (needs OPENAI/LANGSMITH
    keys + an indexed EVAL_REPO — same deferral as prior phases).

- **Phase 9 — Auto-PR** (2026-07-02)
  - `ai/graphs/auto_pr.py` — `plan → generate → commit` `StateGraph` matching the PRD shape:
    `locate → plan → [Send fan-out] generate_file ×N → commit → open_pr` (with a `no_fix`
    branch). `locate` fetches the issue, loads `repo_id` + repo custom rules + the default
    branch, and retrieves related code (repo-scoped, invariant #6) to ground the plan.
    `plan` emits a strict `FixPlan` via `with_structured_output` at **low temperature**
    (`AUTOPR_TEMPERATURE=0`), capped at `AUTOPR_MAX_FILES`. `_fan_out` routes: no files →
    `no_fix`; only deletes → straight to `commit`; else one `generate_file` per create/update
    file. `generate_file` emits the **complete** file contents (no diffs; for updates it
    fetches current contents so the model rewrites the whole file), collected via an
    `operator.add` `generated` reducer. `commit` builds ONE commit on branch
    `ai-fix/issue-<n>` via the Git Data API (`create_tree` from the base tree — create/update
    carry `content`, delete carries a null `sha` — → `create_commit` → `create_ref`).
    `open_pr` opens the PR (`Closes #n`, summary/approach/changes body), comments the PR link
    on the issue, and upserts the `PullRequest` row (`kind=auto-pr`). `no_fix` comments
    honestly instead of opening an empty PR. Custom rules injected into both the plan and
    generate prompts (PRD §F6 relies on §F7). Fresh chat model per call (invariant #3).
  - `app/github/git.py` — Git Data + Pulls **write** helpers (separate from `pulls.py`'s
    read/review): `get_branch_head`, `create_tree`, `create_commit`, `create_ref`,
    `create_pull_request` (returns `OpenedPR{number, html_url}`). Reuses `files.get_file`
    (update context) and `issues.post_issue_comment` (link comment).
  - `ai/llm.py` — `make_chat_model` gained a `temperature` kwarg (passed through to
    `init_chat_model`) so auto-PR can plan/generate deterministically.
  - `ai/prompts.py` — `AUTOPR_PLAN_SYSTEM/HUMAN`, `AUTOPR_GENERATE_SYSTEM/HUMAN`,
    `AUTOPR_RULES_BLOCK`. `ai/constants.py` — `AUTOPR_TEMPERATURE`, `AUTOPR_BRANCH_PREFIX`,
    `AUTOPR_CONTEXT_K`, `AUTOPR_MAX_FILES`.
  - `run_auto_pr` (Celery entrypoint) mirrors `run_issue_analysis`: per-run engine + async
    store injected via `config["configurable"]`, per-run checkpointer, fresh `thread_id`,
    `finally` disposes + `close_redis()`. `app/workers/tasks.py` — `auto_pr` now runs
    `asyncio.run(run_auto_pr(...))` with retries. Webhook already dispatches `auto_pr` on
    `issues labeled auto-fix` (Phase 1) — label-gated, no webhook change.
  - Verified: all graphs + `app.main` import + compile. End-to-end `ainvoke` with fakes
    (mocked GitHub git/issues/files + DB + fake structured model + fake store): full plan
    (create+update+delete) builds correct tree entries (content for create/update, null sha
    for delete), opens PR on `ai-fix/issue-42` into `main` with `Closes #42`, comments the
    PR link, upserts the auto-pr row; deletes-only skips generation and still commits+opens;
    empty plan → `no_fix` comments and opens no PR. (Live OpenAI/GitHub/Postgres deferred.)

- **Phase 8 — Issue Analysis** (2026-07-02)
  - `ai/graphs/issue_analysis.py` — agentic-RAG / ReAct `StateGraph`:
    `prepare → agent ↔ tools → format_post`. `prepare` fetches the issue and loads
    `repo_id` + the repo's custom rules (`ai/rules.load_repo_and_rules`), seeding the
    conversation with the issue as the human turn. `agent` binds `CODEBASE_TOOLS` and
    explores (search → read → grep → follow references); the tool loop is **bounded** by
    `ISSUE_MAX_TOOL_ROUNDS=6` (invariant #10) — at the cap `agent` drops tools so the turn
    ends with a real text comment, never a dangling tool call. Builds a **fresh** chat
    model per call (`make_chat_model`, never the cached singleton — invariant #3).
    `format_post` posts the agent's final message as one issue comment
    (`## 🤖 Revet Issue Analysis` header; a fallback line when the model returns empty) and
    upserts the `Issue` activity row (upsert on repo+number so a re-analysis never dupes).
    Custom rules injected into the system prompt (PRD §F4 AC "custom rules respected", §F7).
  - `ai/tools.py` — `retrieve_code`/`grep_symbol` now read a **per-run store** from
    `config["configurable"]["store"]` via new `_store(config)`, falling back to the cached
    `get_vectorstore()` when none is injected. This resolves the "Tool vectorstore reuse
    under Celery" open question for tool-using graphs: Celery graph runs (issue/auto-PR)
    inject a per-run async store so the tools never bind to a closed loop; chat (FastAPI's
    single loop) injects nothing and keeps the singleton. `retrieve_code` now uses
    `store.asimilarity_search(..., filter={"repo": repo})` directly (repo-scoped, invariant
    #6) instead of `get_retriever`.
  - `app/github/issues.py` — `fetch_issue` (metadata) + `post_issue_comment` (also used for
    the auto-PR link comment, since a PR is an issue on GitHub's REST surface). Boundary
    shape `IssueData` is a frozen dataclass.
  - `ai/prompts.py` — `ISSUE_ANALYSIS_SYSTEM` (rules-injected), `ISSUE_ANALYSIS_RULES_BLOCK`,
    `ISSUE_ANALYSIS_HUMAN`. `ai/constants.py` — `ISSUE_MAX_TOOL_ROUNDS`.
  - `run_issue_analysis` (Celery entrypoint) mirrors `run_pr_review`: per-run engine +
    async store injected via `config["configurable"]`, compiled against a per-run
    `checkpointer()`, fresh `thread_id` (uuid), and `finally` disposes store/engine + calls
    `close_redis()` (the pre–Phase-8 loop-aware-Redis follow-up).
  - `app/workers/tasks.py` — `analyze_issue` now runs `asyncio.run(run_issue_analysis(...))`
    with `autoretry_for`/`retry_backoff` (3 retries), matching `review_pr`. Webhook already
    dispatches `analyze_issue` on `issues opened` (Phase 1) — no webhook change.
  - Verified: all graphs import + compile; chat unaffected by the tools change. End-to-end
    `ainvoke` with fakes (mocked GitHub + DB helpers + fake store + fake model): direct-answer
    path posts a `path:line`-citing comment + writes the `Issue` row; tool-loop path runs the
    tool then answers; empty model output → fallback line; the ReAct loop bounds at exactly
    `ISSUE_MAX_TOOL_ROUNDS=6` tool rounds then emits a final answer. (Live OpenAI/GitHub/
    Postgres run deferred — same as prior phases.)

- **Issues activity feed endpoint** (2026-07-02)
  - `app/api.py` — `GET /repos/{owner}/{repo}/issues` (+ `IssueAnalysisOut`): access-checked
    (`_authorize_repo`) list of the repo's `Issue` activity rows, `updated_at` desc, shaped as
    `{issue_number, state, github_url, created_at, updated_at}` with
    `github_url = https://github.com/{owner}/{repo}/issues/{n}`. Mirrors `GET /pulls`. Backs
    the frontend Phase 8 "Issues" feed. No new persistence (reuses the `Issue` activity row).

- **Reviews activity feed endpoint** (2026-07-01)
  - `app/api.py` — `GET /repos/{owner}/{repo}/pulls` (+ `PullReviewOut` schema): access-checked
    (`_authorize_repo`) list of the repo's `PullRequest` rows where `kind=review`, `updated_at`
    desc, shaped as `{pr_number, state, github_url, created_at, updated_at}` with
    `github_url = https://github.com/{owner}/{repo}/pull/{n}`. Mirrors `list_chat_threads`.
  - Backs the frontend "Reviews" tool (`../revet_fe` `…/pulls`): a read-only feed deep-linking
    to each review on GitHub. Reuses the existing thin activity row — **no new persistence,
    no migration**. The row stores no findings/PR-title, so the feed is intentionally minimal;
    rendering findings in-app would require persisting the rendered review body/findings (deferred).

## In Progress

- None.

- **Pre–Phase-8 PR-review fixes** (2026-07-02) — both mandated fixes landed:
  1. **First-run event-loop error on `review_pr` — fixed at the root.** The last
     cross-loop async singleton the PR-review path reached was the `redis.asyncio`
     client (via `get_installation_token`). A `redis.asyncio` client binds its
     connection pool to the loop it first runs on; each Celery task runs its own
     `asyncio.run()` loop (invariant #3), so the client created on a *prior* task's
     (now-closed) loop made the first command on the new loop raise "Event loop is
     closed" — the pool then evicted the dead connection, which is why the retry
     succeeded. `app/redis_client.py` `get_redis()` is now **loop-aware**: it caches
     the client per running loop and rebuilds when the loop changes, so the first run
     succeeds without the retry. Added `close_redis()` (closes + forgets the client);
     `run_pr_review` calls it in `finally` alongside the engine/store dispose. FastAPI
     (one long-lived loop) is unaffected — it never rebuilds. *Follow-up:* the
     issue/auto-PR entrypoints (Phases 8–9) must call `close_redis()` in `finally`
     too; `run_index` benefits from loop-aware `get_redis` already but doesn't yet
     close on teardown (harmless; tidy up if it ever warns).
  2. **Readable GitHub review output — done.** `_render_review`
     (`ai/graphs/pr_review.py`) now emits a summary line with a per-severity
     breakdown, then a collapsible `<details>` section per severity (critical/high
     expanded, medium/low collapsed), code-span `` `path:line` `` citations, and each
     finding's category + confidence %. Presentation-only — the deterministic
     dedupe/rank/cap in `_dedupe_rank` is untouched (verified the render preserves the
     severity→confidence order).

## Observability — LangSmith wiring (2026-06-28)

- `app/observability.py` — `configure_langsmith()` bridges the `LANGSMITH_*` settings into
  `os.environ` so LangChain/LangGraph auto-trace every graph run. Needed because
  pydantic-settings loads `.env` into the `Settings` object only, while the LangSmith SDK
  reads `os.environ` directly — without the bridge, tracing never activated unless the vars
  were externally exported. Still env-only / no `@traceable` wrappers (per code-standards).
- `app/config.py` — added `langsmith_endpoint` (region host; was present in `.env` but
  unmodeled and silently dropped).
- Wired at both entrypoints: FastAPI lifespan (`app/main.py`) and the Celery worker master
  (`app/workers/celery_app.py`, before prefork so children inherit `os.environ`).
- `env.template` — documented `LANGSMITH_ENDPOINT` + the bridge.

## Next Up

1. **Phase 11 — Custom Rules CRUD API** (per-repo; before Polish — see "Custom Rules (F7)")
5. **Phase 12 — Polish**
6. **Phase 13 — PR close events** (post-v1; see "Post-v1 Phases")
7. **Phase 14 — Install / uninstall repos from the home page** (post-v1)

## Custom Rules (F7) — cross-phase requirements

Custom review rules (PRD §F7) were **partially** implemented in Phase 7: the `Rule`
table exists and the PR-review graph fetches rules and injects them into the
custom-rules reviewer. Three things remain, plus one model change:

- **Model change — rules are now per-repo, not per-installation.** *(Decision
  2026-07-02 — DONE.)* `Rule` moved from an `installation_id` FK to a **`repository_id`
  FK** (`foreign_key="repository.id"`, indexed) so each repo has its own rule set
  (`app/db/models.py`). This is a `create_all` schema change (logged toward the Alembic
  decision, invariant #11). Rule loading was extracted to a shared **`ai/rules.py`
  `load_repo_and_rules(engine, repo)`** (repo-scoped, `"name: body"` texts, capped at
  the new `MAX_RULES=50` constant) so PR review, issue analysis, and auto-PR all inject
  rules the same way. `ai/graphs/pr_review.py` `prepare` now uses it (was loading by
  installation).
- **Injection everywhere (not just PR review).** Custom rules must be fetched (repo-scoped)
  and injected into the relevant prompts of **all** rule-aware features:
  - **PR Review** (Phase 7) — done; re-scoped to the repo via `ai/rules.load_repo_and_rules`.
  - **Issue Analysis** (Phase 8) — **done**; the repo's rules are injected into the ReAct
    agent's system prompt (`ISSUE_ANALYSIS_RULES_BLOCK`) so suggestions respect them
    (PRD §F4 AC: "Custom rules are respected").
  - **Auto-PR** (Phase 9) — **done**; injected into the `plan` and `generate_file` prompts
    (`AUTOPR_RULES_BLOCK`) so generated fixes follow them (PRD §F6 relies on F7).
  A generous fixed cap (e.g. 50) bounds prompt size in every case (PRD §F7).
- **Phase 11 — Custom Rules CRUD API** — per-repo, access-checked REST for managing rules,
  consumed by the frontend Rules tool. Endpoints under `/repos/{owner}/{repo}/rules`:
  `GET` (list the repo's rules), `POST` (create), `PUT`/`PATCH /{rule_id}` (update),
  `DELETE /{rule_id}`. Each runs `get_current_user` → `verify_installation_access` on the
  repo's installation before acting (invariant #13); a rule id is never trusted as a bare
  capability (verify it belongs to the path repo). Scheduled **before Phase 12 — Polish**
  so custom rules are fully manageable end-to-end within v1.

## Post-v1 Phases (after Phase 12 — Polish)

Deferred by request until the core build (Phases 8–11) is complete.

- **Phase 13 — PR close events** — subscribe to the `pull_request` `closed` action
  in the webhook router (`app/github/webhooks.py`). When a closed PR matches a
  `PullRequest` activity row already in our DB (repo + number), change its status to
  closed. Requires adding a lifecycle **state/status column** to `PullRequest` (the
  activity row currently has no state) — a small additive schema change (still
  `create_all`; note for the Alembic decision). No-op when the PR isn't one we track.

- **Phase 14 — Install / uninstall repos from the home page** — let the user
  add/remove repos from our UI instead of returning to GitHub each time.
  **Feasibility (partial, confirmed):** uses the user-to-server token already stored
  in the Redis session (Phase 5). GitHub exposes
  `PUT` / `DELETE /user/installations/{installation_id}/repositories/{repository_id}`
  (add/remove a repo from an *existing* installation); these work with the user OAuth
  token when the user has admin access **and** the installation is in "only selected
  repositories" mode. Backend adds two access-checked endpoints wrapping these (via
  `call_with_refresh`), then upserts / soft-removes the `Repository` row and enqueues
  `index_repo` (or drops the repo's chunks) to match. **Limits the UI must surface:**
  creating the very first installation on an account, and switching an installation
  between "all repos" ↔ "selected repos" mode, still require the GitHub redirect —
  those cannot be done via API. So the home page manages repo membership of an
  existing installation directly and falls back to a GitHub link for first-time
  install / all-repos installations. Frontend counterpart lives in `../revet_fe`.

## Open Questions

- **LLM model choice**: *Decided for chat (Phase 4) and PR review (Phase 6).* Chat
  generation uses the default `settings.llm_model` (gpt-4o); the document grader and query
  rewriter use the cheaper `GRADER_MODEL=openai:gpt-4o-mini`. PR-review **reviewers** use
  `REVIEWER_MODEL=openai:gpt-4o-mini` (cheap, parallel, one structured-output call each).
  The planned "capable aggregator" turned out to be unnecessary: `aggregate` dedupes + ranks
  structured findings, which is a deterministic set/sort operation needing no LLM — so no
  capable model is used in PR review. Revisit (a stronger reviewer model, or an LLM
  aggregator that merges semantically-duplicate findings) only if Phase 9 evals show review
  quality is weak. Changing the reviewer model is a one-constant edit.
- **Migrations**: v1 uses `create_all()`; note when the schema starts churning so we can
  adopt Alembic at the right time. **Churn so far:** `Rule.installation_id → repository_id`
  (2026-07-02, per-repo rules). Two more additive changes are already planned (`PullRequest`
  state for Phase 13, and this Rule FK) — adopt Alembic before the first change that must
  preserve existing rows in a live DB.
- **Embedding dimensions**: PRD specifies `text-embedding-3-small` (1536-dim). If we
  switch embedding models, the vector column dimension must change — this is a breaking
  schema change.
- **GitHub App registration**: App ID and private key must be registered in GitHub before
  Phase 1 can be end-to-end tested.
- **OAuth config (Phase 5)**: the GitHub App needs *"Request user authorization (OAuth)
  during installation"* enabled, a Callback URL, and an OAuth client secret
  (`GITHUB_OAUTH_CLIENT_ID`/`GITHUB_OAUTH_CLIENT_SECRET`) before auth can be tested.
- **User-token lifetime (Phase 5)**: *Implemented (default).* `call_with_refresh`
  (`app/auth/dependencies.py`) refreshes the user token once on a `401` from a user-token
  call and persists it to the session; with no refresh token (the App's non-expiring mode)
  the `401` surfaces as a re-login. Works either way — confirm the App's token-expiry
  setting when OAuth is registered.
- **Session/user-token storage (Phase 5)**: *Decided & implemented.* Sessions live in
  **Redis** keyed by an opaque `session_token`; the `User` row holds only durable identity
  (`github_id`, `login`, `avatar_url`). Refresh tokens live **in the Redis session**, not on
  the `User` row — a session is the unit of token lifetime, and keeping tokens out of the
  relational row avoids encrypted-at-rest token columns (invariant #12). Revisit only if
  cross-session token reuse is ever needed.
- **Callback placement (Phase 5)**: *Backend-agnostic.* `get_current_user` reads the session
  token from either a `session` cookie or `Authorization: Bearer`, so both the
  forward-the-code (frontend owns the cookie) and shared-parent-domain (backend sets the
  cookie) deploys work without backend changes. Still a frontend/deploy decision for where the
  cookie is set.
- **Tool vectorstore reuse under Celery**: `retrieve_code`/`grep_symbol` use the cached
  `get_vectorstore()` singleton (async engine). This is correct for `/chat` (synchronous,
  one event loop) — *confirmed in Phase 4*. For the Celery graph tools (issue/PR/auto-PR, each
  `asyncio.run()`), the module-level async engine can bind to a closed loop (invariant #3).
  *Phase 6 established the pattern for PR review*: `run_pr_review` builds a per-run async
  PGVector store **and** a per-run DB engine, injects them via `config["configurable"]`
  (`store`/`engine`), and the nodes use them directly (`store.asimilarity_search(...,
  filter={"repo": repo})`) instead of the cached singleton; chat models are also built fresh
  per call (`make_chat_model`, not `get_chat_model`). **Resolved for tool-using graphs
  (Phase 8):** `ai/tools.py` `_store(config)` reads the per-run store from
  `config["configurable"]["store"]` (injected by `run_issue_analysis`; auto-PR will inject it
  too), falling back to the cached `get_vectorstore()` only for chat on FastAPI's single loop.
  So `retrieve_code`/`grep_symbol` never reach a closed-loop singleton in a Celery task. The
  redis singleton reached via `get_installation_token` (used by `read_file`/`list_dir`/
  `get_file_tree`) is separately handled by the loop-aware `get_redis()` from the pre–Phase-8
  fix. *(Confirmed the LangGraph checkpointer
  persists only thread/checkpoint ids + serializable state channels, so injecting the
  non-serializable engine/store objects via `configurable` is safe.)*

## Architecture Decisions

- **Access check is a plain function, not a FastAPI dependency** — `verify_installation_access`
  is `await`-ed inside each handler rather than wired as `Depends`, because the
  `installation_id` comes from a path param on `/installations/{id}/...` but is *derived from
  the repo* (DB lookup) on `/repos/{owner}/{repo}/...` and `/chat`. A single uniform
  dependency can't cover both sources, so the check stays an explicit first line in every
  installation/repo handler (still uniform, still invariant #13). Only `get_current_user`
  (no parameters) is a dependency. (Phase 5)
- **PR review posts one COMMENT review, not inline comments** — `format_post` posts a
  single severity-grouped markdown review via the Reviews API with `event="COMMENT"`. The
  PRD allows "single comment or inline"; inline comments require mapping each finding to a
  valid diff position, and the Reviews API rejects the *whole* review if any line isn't part
  of the diff. The summary form is robust, needs no position mapping, and never approves or
  requests changes. (PRD §F5; revisit if inline anchoring is wanted later.) (Phase 6)
- **PR-review fan-out is one parametrized `review` node, not four** — a single `review`
  node fanned out with `Send` over `REVIEW_PERSPECTIVES` (correctness/security/quality/
  custom-rules), each carrying its perspective in the `Send` payload, rather than four
  near-duplicate node functions. This is the idiomatic LangGraph map-reduce shape and keeps
  the reviewers DRY; architecture.md's diagram lists the four perspectives as the logical
  view. custom-rules is only dispatched when the installation has rules. (Phase 6)
- **PR-review aggregation is deterministic** — `aggregate` dedupes (file+line+comment) and
  ranks (severity then confidence) the structured `ReviewFinding`s with plain Python, not an
  LLM, because dedupe+rank is a set/sort operation. The fan-out collects into an
  `operator.add` reducer channel (`findings`); aggregate writes a separate plain `ranked`
  channel (writing back to the reducer channel would *append*, not replace). (Phase 6)
- **Indexing is a plain async pipeline, not a LangGraph graph** — no reasoning step
  needed; adding a graph would be over-engineering. (PRD §F2)
- **Single Postgres for everything** — relational data, pgvector embeddings, and
  LangGraph checkpointer all share one Postgres instance. Simplifies local dev and
  avoids a separate vector service. (PRD §3)
- **Chat is synchronous, not queued** — latency matters; user is waiting. All other AI
  work is async Celery tasks. (PRD §4.2)
- **Chat checkpointer is per-request, graph builder is module-level** — `build_chat_graph()`
  builds the uncompiled graph once; each `/chat` request enters its own
  `checkpointer()` context (its own Postgres connection) and compiles the builder against
  it. Avoids sharing one psycopg connection across concurrent requests while keeping chat
  memory durable per `thread_id`. A connection pool is a possible later optimization if
  per-request connect latency matters. (Phase 4)
- **Chat streaming filters by graph node** — `/chat` streams `stream_mode="messages"` and
  emits only the `generate` node's text chunks, so grader/rewrite LLM tokens and the
  generate loop's tool-call chunks (empty content) never reach the client. Each turn ends
  with a real text answer because `generate` drops its tools once `MAX_TOOL_ROUNDS` is hit.
  (Phase 4)
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
- **Web frontend added; auth is now Phase 5** *(2026-06-27, supersedes "no web frontend
  in v1")* — a Next.js frontend (`../revet_fe`) now consumes the backend. To serve it the
  backend gains a **user-auth layer**: GitHub **OAuth** (the GitHub App's user-to-server
  tokens) backed by a **Redis session**, plus session-gated, access-checked user-facing
  REST endpoints (`/auth/*`, `/me`, `/installations/.../repositories`, repo index/status,
  and a gated `/chat`). This is **lightweight GitHub-only identity** — no passwords, no
  separate accounts system. The **dual-token model** is the core idea: the existing
  **installation token** still does all repo work; the new **user token** only answers
  "who is looking and what can they access" and authorizes requests. The GitHub App must
  enable *"Request user authorization (OAuth) during installation"* and have an OAuth
  client secret. Full contract: `revet_fe/context/github-integration.md`.

## Session Notes

- PRD is at `GENAI_PRD.md` in the repo root.
- No code has been written yet; implementation starts with Phase 0.
- All context files have been populated from the PRD as of 2026-06-19.
