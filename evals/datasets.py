"""Small golden datasets for the eval harness (PRD §F8).

These are intentionally small and **illustrative**. All suites retrieve
repo-scoped code / fetch live PRs & issues, so to score real outputs you must:

  1. Point `EVAL_REPO` at a repository Revet has **already indexed**, and set
     `EVAL_INSTALLATION_ID` to that repo's GitHub App installation id.
  2. Replace the example inputs/`reference_outputs` below with real questions,
     PR numbers, and issue numbers from that repo, plus the expected answers.

Set the repo via env (`EVAL_REPO`, `EVAL_INSTALLATION_ID`) or edit here. Each
example is a langsmith-style dict: ``{"inputs": {...}, "reference_outputs": {...}}``.
"""

import os

EVAL_REPO = os.getenv("EVAL_REPO", "owner/repo")
EVAL_INSTALLATION_ID = int(os.getenv("EVAL_INSTALLATION_ID", "0"))


# Chat Q/A pairs — `reference` is a short model answer used by the groundedness/
# relevance judge (retrieval relevance evaluator).
CHAT_GOLDEN = [
    {
        "inputs": {"question": "How are GitHub webhook signatures verified?"},
        "reference_outputs": {
            "reference": "An HMAC SHA-256 over the raw request body is compared to the "
            "X-Hub-Signature-256 header; a mismatch is rejected with 401 before any dispatch."
        },
    },
    {
        "inputs": {"question": "How are code chunk ids computed so re-indexing is idempotent?"},
        "reference_outputs": {
            "reference": "A deterministic sha over repo + path + line-span, so re-indexing "
            "upserts the same rows instead of duplicating them."
        },
    },
]


# Issues with known target files — the plan/analysis judge checks the suggested
# files overlap `expected_files`.
ISSUE_GOLDEN = [
    {
        "inputs": {
            "issue_number": 0,  # replace with a real issue number in EVAL_REPO
            "title": "Login 500s when email is blank",
            "body": "Submitting the login form with an empty email returns a 500.",
        },
        "reference_outputs": {"expected_files": ["app/auth/login.py"]},
    },
]


# PRs with known issues — the review-usefulness judge checks the review surfaces
# the expected class of problem.
REVIEW_GOLDEN = [
    {
        "inputs": {"pr_number": 0},  # replace with a real PR number in EVAL_REPO
        "reference_outputs": {
            "expected": "should flag the SQL injection built from an unsanitized f-string query."
        },
    },
]


# Issues intended to be auto-fixable — the plan-correctness judge checks the
# FixPlan targets the expected files with a sensible approach.
AUTO_PR_GOLDEN = [
    {
        "inputs": {
            "issue_number": 0,  # replace with a real issue number in EVAL_REPO
            "title": "Typo in README",
            "body": "The README says 'teh' instead of 'the'.",
        },
        "reference_outputs": {"expected_files": ["README.md"]},
    },
]
