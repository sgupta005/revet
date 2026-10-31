# Evals (PRD §F8)

A small LLM-as-judge eval harness over golden datasets, run with
`langsmith.evaluate`. It measures output quality for the AI features and links
results to LangSmith traces.

## Layout

- `datasets.py` — small golden sets: chat Q/A pairs, issues with known target
  files, and PRs with known issues. **Illustrative** — point them at a repo Revet
  has indexed and fill in real numbers/answers (see the module docstring).
- `targets.py` — runs each graph on an example. Review / issue / auto-PR targets
  reuse the **same graph builders** but compile them with `interrupt_before` at the
  posting/commit node, so **evals never post comments, reviews, or commits** — they
  run up to the decision and read the accumulated state. Each injects a per-run
  engine + async vector store (invariant #3). Chat runs end-to-end (no side effects).
- `judges.py` — LLM-as-judge evaluators (retrieval relevance / groundedness, review
  usefulness, auto-PR/issue plan correctness). A cheap judge model (`GRADER_MODEL`,
  temp 0) emits a structured `JudgeVerdict` (0–1 score + reasoning).
- `run_eval.py` — CLI wiring a suite's target + dataset + evaluators into
  `langsmith.evaluate`.

## Running

```bash
export OPENAI_API_KEY=...            # judge + graph models
export LANGSMITH_API_KEY=...         # required by langsmith.evaluate
export LANGSMITH_TRACING=true        # link eval runs to traces
export EVAL_REPO=owner/repo          # a repo Revet has ALREADY indexed
export EVAL_INSTALLATION_ID=12345    # its GitHub App installation id

python -m evals.run_eval chat
python -m evals.run_eval review
python -m evals.run_eval issue
python -m evals.run_eval auto_pr
```

Targets run sequentially (`max_concurrency=1`): each spins its own event loop and
async clients, matching the Celery `asyncio.run` model (invariant #3).
