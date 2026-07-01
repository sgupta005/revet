import logging
import operator
from typing import Annotated, TypedDict
from uuid import uuid4

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import select

from ai.checkpointer import checkpointer
from ai.constants import (
    MAX_DIFF_CHARS,
    MAX_FINDINGS,
    MIN_FINDING_CONFIDENCE,
    REVIEW_CONTEXT_K,
    REVIEW_PERSPECTIVES,
    REVIEW_QUERY_CHARS,
    REVIEWER_MODEL,
)
from ai.llm import make_chat_model, make_embeddings
from ai.prompts import (
    PR_REVIEW_HUMAN,
    PR_REVIEW_PERSPECTIVE_FOCUS,
    PR_REVIEW_RULES_BLOCK,
    PR_REVIEW_SYSTEM,
)
from ai.retriever import format_doc
from ai.rules import load_repo_and_rules
from ai.schemas import ReviewFinding, ReviewFindings
from ai.vectorstore import make_vectorstore
from app.db.models import PRKind, PullRequest
from app.db.session import build_engine
from app.github.auth import get_installation_token
from app.github.constants import GITHUB_API
from app.github.pulls import PRFile, fetch_pull_request, post_review
from app.redis_client import close_redis

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_LABEL = {
    "critical": "🔴 Critical",
    "high": "🟠 High",
    "medium": "🟡 Medium",
    "low": "🔵 Low",
}


class PRReviewState(TypedDict):
    """Multi-agent fan-out review state. `findings` uses an `operator.add` reducer so
    the parallel reviewer nodes can merge their structured outputs; `ranked` holds the
    deterministic dedupe+rank result (a plain channel — aggregate overwrites it rather
    than appending to the reducer)."""

    title: str
    body: str
    pr_state: str
    diff: str
    changed_files: list[str]
    repo_id: int | None
    rules: list[str]
    context: str
    findings: Annotated[list[ReviewFinding], operator.add]
    ranked: list[ReviewFinding]
    review_body: str


class ReviewTask(TypedDict):
    """The slice of state a single fanned-out reviewer receives via `Send`."""

    perspective: str
    title: str
    body: str
    diff: str
    context: str
    rules: list[str]


def _build_diff(files: list[PRFile]) -> str:
    """Render changed files into one capped diff string; oversized PRs are truncated
    so the reviewer prompt stays within a sane token budget (MAX_DIFF_CHARS)."""
    parts: list[str] = []
    used = 0
    for f in files:
        if not f.patch:
            parts.append(f"### {f.path} ({f.status}, no textual diff)")
            continue
        block = f"### {f.path} ({f.status})\n```diff\n{f.patch}\n```"
        if used + len(block) > MAX_DIFF_CHARS:
            parts.append("… diff truncated (PR too large to include in full) …")
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


async def prepare(state: PRReviewState, config: RunnableConfig) -> dict:
    """Fetch the PR (title/body/diff/files) and load repo id + custom rules — the
    shared inputs every reviewer fans out over."""
    cfg = config["configurable"]
    repo, installation_id, pr_number = cfg["repo"], cfg["installation_id"], cfg["pr_number"]
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        pr = await fetch_pull_request(client, repo, pr_number, token)
    repo_id, rules = await load_repo_and_rules(cfg["engine"], repo)
    return {
        "title": pr.title,
        "body": pr.body,
        "pr_state": pr.state,
        "diff": _build_diff(pr.files),
        "changed_files": [f.path for f in pr.files],
        "repo_id": repo_id,
        "rules": rules,
    }


async def retrieve_context(state: PRReviewState, config: RunnableConfig) -> dict:
    """Repo-scoped semantic retrieval of code related to the changed files, giving
    reviewers context beyond the diff (invariant #6: always filtered to the repo)."""
    cfg = config["configurable"]
    if not state["changed_files"]:
        return {"context": ""}
    query = ("\n".join(state["changed_files"]) + "\n\n" + state["diff"])[:REVIEW_QUERY_CHARS]
    docs = await cfg["store"].asimilarity_search(
        query, k=REVIEW_CONTEXT_K, filter={"repo": cfg["repo"]}
    )
    return {"context": "\n\n---\n\n".join(format_doc(d) for d in docs)}


def _fan_out(state: PRReviewState) -> list[Send]:
    """Dispatch the parallel reviewers (PRD §F5); custom-rules only runs when the
    installation actually has rules to enforce."""
    task: ReviewTask = {
        "perspective": "",
        "title": state["title"],
        "body": state["body"],
        "diff": state["diff"],
        "context": state["context"],
        "rules": state["rules"],
    }
    sends = []
    for perspective in REVIEW_PERSPECTIVES:
        if perspective == "custom-rules" and not state["rules"]:
            continue
        sends.append(Send("review", {**task, "perspective": perspective}))
    return sends


def _review_human(task: ReviewTask) -> str:
    """Build the reviewer's user message, appending the rules block only for the
    custom-rules perspective."""
    message = PR_REVIEW_HUMAN.format(
        title=task["title"],
        body=task["body"] or "(no description)",
        diff=task["diff"] or "(empty diff)",
        context=task["context"] or "(no related code retrieved)",
    )
    if task["perspective"] == "custom-rules":
        rules = "\n".join(f"- {r}" for r in task["rules"])
        message += PR_REVIEW_RULES_BLOCK.format(rules=rules)
    return message


async def review(task: ReviewTask, config: RunnableConfig) -> dict:
    """One perspective's reviewer: emits structured `list[ReviewFinding]`. Builds a
    fresh chat model per call (never a cached one) because Celery tasks run their own
    event loop per `asyncio.run` (invariant #3)."""
    perspective = task["perspective"]
    model = make_chat_model(REVIEWER_MODEL).with_structured_output(ReviewFindings)
    system = PR_REVIEW_SYSTEM.format(
        perspective=perspective, focus=PR_REVIEW_PERSPECTIVE_FOCUS[perspective]
    )
    result: ReviewFindings = await model.ainvoke(
        [SystemMessage(system), HumanMessage(_review_human(task))], config
    )
    return {"findings": result.findings}


def _dedupe_rank(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Drop low-confidence findings, dedupe identical comments on the same line, then
    rank by severity and confidence and cap the count (PRD §F5: aggregate dedupes + ranks)."""
    kept: dict[tuple[str, int, str], ReviewFinding] = {}
    for f in findings:
        if f.confidence < MIN_FINDING_CONFIDENCE:
            continue
        key = (f.file, f.line, f.comment.strip().lower())
        if key not in kept or f.confidence > kept[key].confidence:
            kept[key] = f
    ranked = sorted(
        kept.values(), key=lambda f: (SEVERITY_RANK.get(f.severity, 99), -f.confidence)
    )
    return ranked[:MAX_FINDINGS]


async def aggregate(state: PRReviewState) -> dict:
    """Deterministically dedupe + rank the fanned-out findings into the final set."""
    return {"ranked": _dedupe_rank(state["findings"])}


def _render_review(ranked: list[ReviewFinding], changed_files: list[str]) -> str:
    """Render one markdown review body: a summary line with a per-severity
    breakdown, then a collapsible `<details>` section per severity (critical/high
    expanded, medium/low collapsed) with code-span `path:line` citations and each
    finding's category + confidence. Presentation only — `ranked` is already
    deterministically deduped, ranked (severity then confidence), and capped by
    `_dedupe_rank`; this function only groups and formats it, preserving that order."""
    header = "## 🤖 Revet AI Review"
    n_files = len(changed_files)
    if not ranked:
        return f"{header}\n\n✅ No issues found across **{n_files}** changed file(s)."

    counts: dict[str, int] = {}
    for f in ranked:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    # SEVERITY_RANK iterates critical→low (insertion order), matching the ranking.
    breakdown = " · ".join(
        f"{SEVERITY_LABEL[s].split()[0]} {counts[s]} {s}"
        for s in SEVERITY_RANK
        if counts.get(s)
    )
    lines = [
        header,
        "",
        f"Found **{len(ranked)}** issue(s) across **{n_files}** changed file(s) — {breakdown}.",
    ]
    for severity in SEVERITY_RANK:
        group = [f for f in ranked if f.severity == severity]
        if not group:
            continue
        label = SEVERITY_LABEL.get(severity, severity)
        open_attr = " open" if severity in ("critical", "high") else ""
        lines.append("")
        lines.append(f"<details{open_attr}>")
        lines.append(f"<summary>{label} ({len(group)})</summary>")
        lines.append("")
        for f in group:
            conf = round(f.confidence * 100)
            lines.append(
                f"- **`{f.file}:{f.line}`** · {f.category} · {conf}% — {f.comment}"
            )
        lines.append("")
        lines.append("</details>")
    return "\n".join(lines)


async def _upsert_pr_row(
    engine: AsyncEngine, repo_id: int, pr_number: int, pr_state: str
) -> None:
    """Write/refresh the `PullRequest` activity row for this review; upserts on
    (repo, number, kind=review) so a re-review on `synchronize` never duplicates."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        row = (
            await session.execute(
                select(PullRequest).where(
                    PullRequest.repo_id == repo_id,
                    PullRequest.github_pr_number == pr_number,
                    PullRequest.kind == PRKind.REVIEW,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = PullRequest(
                repo_id=repo_id,
                github_pr_number=pr_number,
                kind=PRKind.REVIEW,
                state=pr_state,
            )
        else:
            row.state = pr_state
        session.add(row)
        await session.commit()


async def format_post(state: PRReviewState, config: RunnableConfig) -> dict:
    """Render the review, post it as one PR review, and write the activity row."""
    cfg = config["configurable"]
    repo, installation_id, pr_number = cfg["repo"], cfg["installation_id"], cfg["pr_number"]
    body = _render_review(state["ranked"], state["changed_files"])
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        await post_review(client, repo, pr_number, body, token)
    if state["repo_id"] is not None:
        await _upsert_pr_row(cfg["engine"], state["repo_id"], pr_number, state["pr_state"])
    logger.info(
        "review_pr posted repo=%s pr=%s findings=%d",
        repo,
        pr_number,
        len(state["ranked"]),
    )
    return {"review_body": body}


def build_pr_review_graph() -> StateGraph:
    """Build the uncompiled multi-agent PR-review graph: prepare → retrieve_context →
    [Send fan-out] review ×N → aggregate → format_post."""
    builder = StateGraph(PRReviewState)
    builder.add_node("prepare", prepare)
    builder.add_node("retrieve_context", retrieve_context)
    builder.add_node("review", review)
    builder.add_node("aggregate", aggregate)
    builder.add_node("format_post", format_post)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "retrieve_context")
    builder.add_conditional_edges("retrieve_context", _fan_out, ["review"])
    builder.add_edge("review", "aggregate")
    builder.add_edge("aggregate", "format_post")
    builder.add_edge("format_post", END)
    return builder


_graph_builder = build_pr_review_graph()


async def run_pr_review(repo: str, installation_id: int, pr_number: int) -> None:
    """Celery entrypoint: build per-run DB engine + vector store (prefork-safe,
    invariant #3), compile the graph against a checkpointer, and run one review. A
    fresh thread_id per run keeps the `operator.add` findings reducer from carrying
    over between reviews of the same PR."""
    engine = build_engine()
    store = make_vectorstore(make_embeddings(), async_mode=True)
    try:
        async with checkpointer() as saver:
            graph = _graph_builder.compile(checkpointer=saver)
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": f"pr_review:{repo}:{pr_number}:{uuid4()}",
                    "repo": repo,
                    "installation_id": installation_id,
                    "pr_number": pr_number,
                    "engine": engine,
                    "store": store,
                }
            }
            await graph.ainvoke({"findings": []}, config)
    finally:
        await close_redis()
        await store._async_engine.dispose()
        await engine.dispose()
