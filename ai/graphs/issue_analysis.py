import logging
from typing import Annotated, TypedDict
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import select

from ai.checkpointer import checkpointer
from ai.constants import ISSUE_MAX_TOOL_ROUNDS
from ai.llm import make_chat_model, make_embeddings
from ai.prompts import (
    ISSUE_ANALYSIS_HUMAN,
    ISSUE_ANALYSIS_RULES_BLOCK,
    ISSUE_ANALYSIS_SYSTEM,
)
from ai.rules import load_repo_and_rules
from ai.tools import CODEBASE_TOOLS
from ai.vectorstore import make_vectorstore
from app.db.models import Issue
from app.db.session import build_engine
from app.github.auth import get_installation_token
from app.github.constants import GITHUB_API
from app.github.issues import fetch_issue, post_issue_comment
from app.redis_client import close_redis

logger = logging.getLogger(__name__)

COMMENT_HEADER = "## 🤖 Revet Issue Analysis"
_FALLBACK = "I couldn't find enough relevant code in this repository to analyze this issue."


class IssueState(TypedDict):
    """Agentic-RAG issue-analysis state. `messages` holds the ReAct exploration
    (human issue → tool-calling AI turns ↔ tool results → final AI comment)."""

    messages: Annotated[list[AnyMessage], add_messages]
    issue_state: str
    repo_id: int | None
    rules: list[str]
    comment: str


def _tool_rounds(messages: list[AnyMessage]) -> int:
    """Count tool-calling AI turns since the last human message; bounds the ReAct
    loop so the agent cannot call tools indefinitely (invariant #10)."""
    rounds = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and message.tool_calls:
            rounds += 1
    return rounds


def _system_prompt(rules: list[str]) -> str:
    """Issue-analysis system prompt with the repo's custom rules injected (PRD §F7)."""
    block = (
        ISSUE_ANALYSIS_RULES_BLOCK.format(rules="\n".join(f"- {r}" for r in rules))
        if rules
        else ""
    )
    return ISSUE_ANALYSIS_SYSTEM.format(rules=block)


async def prepare(state: IssueState, config: RunnableConfig) -> dict:
    """Fetch the issue and load repo id + custom rules; seed the conversation with
    the issue as the human turn."""
    cfg = config["configurable"]
    repo, installation_id, issue_number = (
        cfg["repo"],
        cfg["installation_id"],
        cfg["issue_number"],
    )
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        issue = await fetch_issue(client, repo, issue_number, token)
    repo_id, rules = await load_repo_and_rules(cfg["engine"], repo)
    human = ISSUE_ANALYSIS_HUMAN.format(
        title=issue.title, body=issue.body or "(no description)"
    )
    return {
        "issue_state": issue.state,
        "repo_id": repo_id,
        "rules": rules,
        "messages": [HumanMessage(human)],
    }


async def agent(state: IssueState, config: RunnableConfig) -> dict:
    """ReAct step: bind the codebase tools and explore, until the tool budget is
    spent — then answer without tools so the turn ends with the final comment
    (never a dangling tool call). A fresh chat model per call (never a cached
    singleton) keeps it safe under the Celery task's own event loop (invariant #3)."""
    base = make_chat_model()
    model = (
        base.bind_tools(CODEBASE_TOOLS)
        if _tool_rounds(state["messages"]) < ISSUE_MAX_TOOL_ROUNDS
        else base
    )
    prompt = [SystemMessage(_system_prompt(state["rules"])), *state["messages"]]
    response = await model.ainvoke(prompt, config)
    return {"messages": [response]}


def _route_after_agent(state: IssueState) -> str:
    """Loop to tools while the agent requests them; otherwise post the comment."""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "format_post"


async def _upsert_issue_row(
    engine: AsyncEngine, repo_id: int, issue_number: int, state: str
) -> None:
    """Write/refresh the `Issue` activity row; upserts on (repo, number) so a
    re-analysis never duplicates."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Issue).where(
                    Issue.repo_id == repo_id,
                    Issue.github_issue_number == issue_number,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = Issue(
                repo_id=repo_id, github_issue_number=issue_number, state=state
            )
        else:
            row.state = state
        session.add(row)
        await session.commit()


async def format_post(state: IssueState, config: RunnableConfig) -> dict:
    """Post the agent's analysis as an issue comment and write the activity row."""
    cfg = config["configurable"]
    repo, installation_id, issue_number = (
        cfg["repo"],
        cfg["installation_id"],
        cfg["issue_number"],
    )
    last = state["messages"][-1]
    analysis = last.content if isinstance(last, AIMessage) and isinstance(last.content, str) else ""
    body = f"{COMMENT_HEADER}\n\n{analysis.strip() or _FALLBACK}"
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=30) as client:
        await post_issue_comment(client, repo, issue_number, body, token)
    if state["repo_id"] is not None:
        await _upsert_issue_row(
            cfg["engine"], state["repo_id"], issue_number, state["issue_state"]
        )
    logger.info("analyze_issue posted repo=%s issue=%s", repo, issue_number)
    return {"comment": body}


def build_issue_analysis_graph() -> StateGraph:
    """Build the uncompiled agentic-RAG issue graph: prepare → agent ↔ tools →
    format_post."""
    builder = StateGraph(IssueState)
    builder.add_node("prepare", prepare)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(CODEBASE_TOOLS))
    builder.add_node("format_post", format_post)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "agent")
    builder.add_conditional_edges("agent", _route_after_agent, ["tools", "format_post"])
    builder.add_edge("tools", "agent")
    builder.add_edge("format_post", END)
    return builder


_graph_builder = build_issue_analysis_graph()


async def run_issue_analysis(repo: str, installation_id: int, issue_number: int) -> None:
    """Celery entrypoint: build a per-run DB engine + vector store (prefork-safe,
    invariant #3), inject them (the codebase tools read the store from config so
    they never reach the cached singleton), compile against a checkpointer, and run
    one analysis with a fresh thread_id."""
    engine = build_engine()
    store = make_vectorstore(make_embeddings(), async_mode=True)
    try:
        async with checkpointer() as saver:
            graph = _graph_builder.compile(checkpointer=saver)
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": f"issue:{repo}:{issue_number}:{uuid4()}",
                    "repo": repo,
                    "installation_id": installation_id,
                    "issue_number": issue_number,
                    "engine": engine,
                    "store": store,
                }
            }
            await graph.ainvoke({"messages": []}, config)
    finally:
        await close_redis()
        await store._async_engine.dispose()
        await engine.dispose()
