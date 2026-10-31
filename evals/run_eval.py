"""Eval runner (PRD §F8): score graph outputs on the golden datasets with
`langsmith.evaluate` + LLM-as-judge evaluators.

Usage:
    python -m evals.run_eval chat        # retrieval relevance / groundedness
    python -m evals.run_eval review      # review usefulness
    python -m evals.run_eval issue       # issue-analysis plan correctness
    python -m evals.run_eval auto_pr     # auto-PR plan correctness

Requires: OPENAI_API_KEY, LANGSMITH_API_KEY (+ LANGSMITH_TRACING=true to link
runs), and an indexed EVAL_REPO / EVAL_INSTALLATION_ID (see evals/datasets.py).
Targets run sequentially (max_concurrency=1) because each spins its own event
loop / async clients (invariant #3).
"""

import argparse

from langsmith import evaluate

from evals import datasets, judges, targets

SUITES = {
    "chat": (targets.chat_target, datasets.CHAT_GOLDEN, [judges.retrieval_relevance]),
    "review": (targets.review_target, datasets.REVIEW_GOLDEN, [judges.review_usefulness]),
    "issue": (targets.issue_target, datasets.ISSUE_GOLDEN, [judges.plan_correctness]),
    "auto_pr": (targets.auto_pr_target, datasets.AUTO_PR_GOLDEN, [judges.plan_correctness]),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Revet eval suite.")
    parser.add_argument("suite", choices=sorted(SUITES))
    args = parser.parse_args()

    target, data, evaluators = SUITES[args.suite]
    results = evaluate(
        target,
        data=data,
        evaluators=evaluators,
        experiment_prefix=f"revet-{args.suite}",
        max_concurrency=1,
    )
    print(results)


if __name__ == "__main__":
    main()
