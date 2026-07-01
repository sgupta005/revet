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
    AUTOPR_BRANCH_PREFIX,
    AUTOPR_CONTEXT_K,
    AUTOPR_MAX_FILES,
    AUTOPR_TEMPERATURE,
)
from ai.llm import make_chat_model, make_embeddings
from ai.prompts import (
    AUTOPR_GENERATE_HUMAN,
    AUTOPR_GENERATE_SYSTEM,
    AUTOPR_PLAN_HUMAN,
    AUTOPR_PLAN_SYSTEM,
    AUTOPR_RULES_BLOCK,
)
from ai.retriever import format_doc
from ai.rules import load_repo_and_rules
from ai.schemas import FixPlan
from ai.vectorstore import make_vectorstore
from app.db.models import PRKind, PullRequest
from app.db.session import build_engine
from app.github.auth import get_installation_token
from app.github.constants import GITHUB_API
from app.github.files import get_default_branch, get_file
from app.github.git import (
    create_commit,
    create_pull_request,
    create_ref,
    create_tree,
    get_branch_head,
)
from app.github.issues import fetch_issue, post_issue_comment
from app.redis_client import close_redis

logger = logging.getLogger(__name__)

BLOB_MODE = "100644"


class AutoPRState(TypedDict):
    """Plan→generate→commit state. `generated` collects the fanned-out per-file
    contents via an `operator.add` reducer (like pr_review's findings)."""

    number: int
    title: str
    body: str
    repo_id: int | None
    rules: list[str]
    base_branch: str
    context: str
    plan: FixPlan | None
    generated: Annotated[list[tuple[str, str]], operator.add]
    pr_url: str


class GenerateTask(TypedDict):
    """The slice a single fanned-out generate_file receives via `Send`."""

    number: int
    title: str
    summary: str
    approach: str
    path: str
    action: str
    rationale: str
    rules: list[str]
    base_branch: str


def _rules_block(rules: list[str]) -> str:
    return AUTOPR_RULES_BLOCK.format(rules="\n".join(f"- {r}" for r in rules)) if rules else ""


async def locate(state: AutoPRState, config: RunnableConfig) -> dict:
    """Fetch the issue, load repo id + custom rules + the default branch, and
    retrieve related code (repo-scoped, invariant #6) to ground the plan."""
    cfg = config["configurable"]
    repo, installation_id, issue_number = (
        cfg["repo"],
        cfg["installation_id"],
        cfg["issue_number"],
    )
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        issue = await fetch_issue(client, repo, issue_number, token)
        base_branch = await get_default_branch(client, repo, token)
    repo_id, rules = await load_repo_and_rules(cfg["engine"], repo)
    query = f"{issue.title}\n\n{issue.body}"
    docs = await cfg["store"].asimilarity_search(
        query, k=AUTOPR_CONTEXT_K, filter={"repo": repo}
    )
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "repo_id": repo_id,
        "rules": rules,
        "base_branch": base_branch,
        "context": "\n\n---\n\n".join(format_doc(d) for d in docs) or "(no related code retrieved)",
    }


async def plan(state: AutoPRState, config: RunnableConfig) -> dict:
    """Produce a strict `FixPlan` (summary, approach, files[]) at low temperature;
    caps the file list so a single fix PR never fans out unbounded."""
    model = make_chat_model(temperature=AUTOPR_TEMPERATURE).with_structured_output(FixPlan)
    system = AUTOPR_PLAN_SYSTEM.format(rules=_rules_block(state["rules"]))
    human = AUTOPR_PLAN_HUMAN.format(
        number=state["number"],
        title=state["title"],
        body=state["body"] or "(no description)",
        context=state["context"],
    )
    result: FixPlan = await model.ainvoke([SystemMessage(system), HumanMessage(human)], config)
    result.files = result.files[:AUTOPR_MAX_FILES]
    return {"plan": result}


def _fan_out(state: AutoPRState):
    """Route after planning: no files → no_fix; only deletes → straight to commit;
    otherwise fan out one generate_file per create/update file."""
    fixplan = state["plan"]
    if fixplan is None or not fixplan.files:
        return "no_fix"
    to_write = [f for f in fixplan.files if f.action in ("create", "update")]
    if not to_write:
        return "commit"  # deletes only — nothing to generate
    return [
        Send(
            "generate_file",
            GenerateTask(
                number=state["number"],
                title=state["title"],
                summary=fixplan.summary,
                approach=fixplan.approach,
                path=f.path,
                action=f.action,
                rationale=f.rationale,
                rules=state["rules"],
                base_branch=state["base_branch"],
            ),
        )
        for f in to_write
    ]


async def generate_file(task: GenerateTask, config: RunnableConfig) -> dict:
    """Generate the complete final contents of one file. For updates, the current
    contents are fetched and shown so the model rewrites the whole file (never a
    diff). Fresh chat model per call (invariant #3)."""
    cfg = config["configurable"]
    repo, installation_id = cfg["repo"], cfg["installation_id"]
    current = ""
    if task["action"] == "update":
        token = await get_installation_token(installation_id)
        async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
            existing = await get_file(client, repo, task["path"], task["base_branch"], token)
        current = existing.content if existing else ""
    model = make_chat_model(temperature=AUTOPR_TEMPERATURE)
    system = AUTOPR_GENERATE_SYSTEM.format(rules=_rules_block(task["rules"]))
    human = AUTOPR_GENERATE_HUMAN.format(
        number=task["number"],
        title=task["title"],
        summary=task["summary"],
        approach=task["approach"],
        action=task["action"],
        path=task["path"],
        rationale=task["rationale"],
        current=current or "(new file)",
    )
    response = await model.ainvoke([SystemMessage(system), HumanMessage(human)], config)
    content = response.content if isinstance(response.content, str) else str(response.content)
    return {"generated": [(task["path"], content)]}


def _tree_entries(plan: FixPlan, generated: dict[str, str]) -> list[dict]:
    """Build Git-Data tree entries: create/update carry full `content`; delete
    carries a null `sha` (removes the path from the base tree)."""
    entries: list[dict] = []
    for f in plan.files:
        if f.action == "delete":
            entries.append({"path": f.path, "mode": BLOB_MODE, "type": "blob", "sha": None})
        elif f.path in generated:
            entries.append(
                {"path": f.path, "mode": BLOB_MODE, "type": "blob", "content": generated[f.path]}
            )
    return entries


async def commit(state: AutoPRState, config: RunnableConfig) -> dict:
    """Build one commit on a new branch via the Git Data API (tree from base tree
    with the changed/deleted entries → commit → branch ref)."""
    cfg = config["configurable"]
    repo, installation_id = cfg["repo"], cfg["installation_id"]
    fixplan = state["plan"]
    generated = dict(state.get("generated") or [])
    entries = _tree_entries(fixplan, generated)
    branch = f"{AUTOPR_BRANCH_PREFIX}{state['number']}"
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        parent_sha, base_tree = await get_branch_head(client, repo, state["base_branch"], token)
        tree_sha = await create_tree(client, repo, base_tree, entries, token)
        commit_sha = await create_commit(
            client, repo, f"AI fix for issue #{state['number']}: {fixplan.summary}", tree_sha, parent_sha, token
        )
        await create_ref(client, repo, branch, commit_sha, token)
    logger.info("auto_pr committed repo=%s branch=%s files=%d", repo, branch, len(entries))
    return {}


def _pr_body(state: AutoPRState) -> str:
    """PR description: closes the issue, states the approach, lists the changes."""
    fixplan = state["plan"]
    changes = "\n".join(f"- `{f.path}` ({f.action}) — {f.rationale}" for f in fixplan.files)
    return (
        f"🤖 Automated fix generated by Revet for #{state['number']}.\n\n"
        f"Closes #{state['number']}\n\n"
        f"**Summary:** {fixplan.summary}\n\n"
        f"**Approach:** {fixplan.approach}\n\n"
        f"**Changes**\n{changes}\n\n"
        "> This PR is bot-generated and is not auto-merged — review before merging."
    )


async def _upsert_pr_row(engine: AsyncEngine, repo_id: int, pr_number: int) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        row = (
            await session.execute(
                select(PullRequest).where(
                    PullRequest.repo_id == repo_id,
                    PullRequest.github_pr_number == pr_number,
                    PullRequest.kind == PRKind.AUTO_PR,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = PullRequest(
                repo_id=repo_id, github_pr_number=pr_number, kind=PRKind.AUTO_PR, state="open"
            )
        else:
            row.state = "open"
        session.add(row)
        await session.commit()


async def open_pr(state: AutoPRState, config: RunnableConfig) -> dict:
    """Open the PR, comment the link on the issue, and write the auto-PR activity row."""
    cfg = config["configurable"]
    repo, installation_id = cfg["repo"], cfg["installation_id"]
    branch = f"{AUTOPR_BRANCH_PREFIX}{state['number']}"
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        pr = await create_pull_request(
            client,
            repo,
            title=f"AI fix: {state['plan'].summary}",
            head=branch,
            base=state["base_branch"],
            body=_pr_body(state),
            token=token,
        )
        await post_issue_comment(
            client,
            repo,
            state["number"],
            f"🤖 Opened a fix PR for this issue: {pr.html_url}",
            token,
        )
    if state["repo_id"] is not None:
        await _upsert_pr_row(cfg["engine"], state["repo_id"], pr.number)
    logger.info("auto_pr opened repo=%s issue=%s pr=%s", repo, state["number"], pr.number)
    return {"pr_url": pr.html_url}


async def no_fix(state: AutoPRState, config: RunnableConfig) -> dict:
    """The planner produced no file changes — comment honestly instead of opening
    an empty PR."""
    cfg = config["configurable"]
    repo, installation_id = cfg["repo"], cfg["installation_id"]
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        await post_issue_comment(
            client,
            repo,
            state["number"],
            "🤖 Revet couldn't determine a concrete code change for this issue, so no fix PR was opened.",
            token,
        )
    logger.info("auto_pr no-fix repo=%s issue=%s", repo, state["number"])
    return {"pr_url": ""}


def build_auto_pr_graph() -> StateGraph:
    """Build the uncompiled auto-PR graph: locate → plan → [fan-out] generate_file
    → commit → open_pr (or no_fix)."""
    builder = StateGraph(AutoPRState)
    builder.add_node("locate", locate)
    builder.add_node("plan", plan)
    builder.add_node("generate_file", generate_file)
    builder.add_node("commit", commit)
    builder.add_node("open_pr", open_pr)
    builder.add_node("no_fix", no_fix)

    builder.add_edge(START, "locate")
    builder.add_edge("locate", "plan")
    builder.add_conditional_edges("plan", _fan_out, ["generate_file", "commit", "no_fix"])
    builder.add_edge("generate_file", "commit")
    builder.add_edge("commit", "open_pr")
    builder.add_edge("open_pr", END)
    builder.add_edge("no_fix", END)
    return builder


_graph_builder = build_auto_pr_graph()


async def run_auto_pr(repo: str, installation_id: int, issue_number: int) -> None:
    """Celery entrypoint (label-gated `auto-fix` — dispatched by the webhook): per-run
    engine + store injected via configurable (invariant #3), compiled against a
    per-run checkpointer with a fresh thread_id; disposes resources + closes redis."""
    engine = build_engine()
    store = make_vectorstore(make_embeddings(), async_mode=True)
    try:
        async with checkpointer() as saver:
            graph = _graph_builder.compile(checkpointer=saver)
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": f"auto_pr:{repo}:{issue_number}:{uuid4()}",
                    "repo": repo,
                    "installation_id": installation_id,
                    "issue_number": issue_number,
                    "engine": engine,
                    "store": store,
                }
            }
            await graph.ainvoke({"generated": []}, config)
    finally:
        await close_redis()
        await store._async_engine.dispose()
        await engine.dispose()
