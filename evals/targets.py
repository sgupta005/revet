"""Eval targets: run each graph on an example and return its output.

Side-effecting graphs (review/issue/auto-PR) are compiled with `interrupt_before`
at their posting/commit node and an in-memory checkpointer, so **evals never post
comments, reviews, or commits** — they run up to the decision and read the
accumulated state. Each target injects a per-run engine + async vector store
(invariant #3) and disposes them, exactly like the Celery entrypoints.

Chat has no external side effects, so it runs end-to-end; its singleton caches are
cleared first so each `asyncio.run` binds fresh clients to its own loop.
"""

import asyncio
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from ai.llm import get_chat_model, get_embeddings, make_embeddings
from ai.vectorstore import get_vectorstore, make_vectorstore
from evals.datasets import EVAL_INSTALLATION_ID, EVAL_REPO


def _base_config(thread_prefix: str, engine=None, store=None) -> RunnableConfig:
    cfg: dict = {
        "thread_id": f"eval:{thread_prefix}:{uuid4()}",
        "repo": EVAL_REPO,
        "installation_id": EVAL_INSTALLATION_ID,
    }
    if engine is not None:
        cfg["engine"] = engine
    if store is not None:
        cfg["store"] = store
    return {"configurable": cfg}


def _reset_chat_singletons() -> None:
    """Chat uses the cached vectorstore/embeddings/chat-model singletons; clear
    them so a fresh `asyncio.run` loop rebinds its own async clients."""
    get_vectorstore.cache_clear()
    get_embeddings.cache_clear()
    get_chat_model.cache_clear()


def chat_target(inputs: dict) -> dict:
    """Run the chat graph end-to-end and return the answer + retrieved docs."""
    from ai.graphs.chat import build_chat_graph

    async def _run() -> dict:
        graph = build_chat_graph().compile(checkpointer=MemorySaver())
        state = await graph.ainvoke(
            {"messages": [HumanMessage(inputs["question"])], "query": "", "rewrites": 0},
            _base_config("chat"),
        )
        last = state["messages"][-1]
        answer = last.content if isinstance(last, AIMessage) else ""
        return {"answer": answer, "documents": state.get("documents", [])}

    _reset_chat_singletons()
    return asyncio.run(_run())


async def _run_interrupted(
    build, stop_before: list[str], initial: dict, thread_prefix: str
) -> dict:
    """Compile a side-effecting graph with an interrupt before its write node(s),
    run to that point with per-run engine/store, and return the state values."""
    from app.db.session import build_engine

    engine = build_engine()
    store = make_vectorstore(make_embeddings(), async_mode=True)
    try:
        graph = build().compile(checkpointer=MemorySaver(), interrupt_before=stop_before)
        config = _base_config(thread_prefix, engine=engine, store=store)
        await graph.ainvoke(initial, config)
        snapshot = await graph.aget_state(config)
        return dict(snapshot.values)
    finally:
        await store._async_engine.dispose()
        await engine.dispose()


def review_target(inputs: dict) -> dict:
    """Run the PR-review graph up to (but not including) posting; return findings."""
    from ai.graphs.pr_review import build_pr_review_graph

    async def _run() -> dict:
        values = await _run_interrupted(
            build_pr_review_graph, ["format_post"], {"findings": []},
            f"review:{inputs['pr_number']}",
        )
        ranked = values.get("ranked", [])
        return {
            "findings": [
                {"file": f.file, "line": f.line, "severity": f.severity,
                 "category": f.category, "comment": f.comment}
                for f in ranked
            ]
        }

    return asyncio.run(_run())


def issue_target(inputs: dict) -> dict:
    """Run the issue-analysis graph up to (but not including) posting; return the
    analysis text the agent would have commented."""
    from ai.graphs.issue_analysis import build_issue_analysis_graph

    async def _run() -> dict:
        values = await _run_interrupted(
            build_issue_analysis_graph, ["format_post"], {"messages": []},
            f"issue:{inputs['issue_number']}",
        )
        msgs = values.get("messages", [])
        last = msgs[-1] if msgs else None
        answer = last.content if isinstance(last, AIMessage) else ""
        return {"answer": answer, "files": answer}

    return asyncio.run(_run())


def auto_pr_target(inputs: dict) -> dict:
    """Run the auto-PR graph up to (but not including) commit; return the FixPlan."""
    from ai.graphs.auto_pr import build_auto_pr_graph

    async def _run() -> dict:
        values = await _run_interrupted(
            build_auto_pr_graph, ["commit", "no_fix"], {"generated": []},
            f"auto_pr:{inputs['issue_number']}",
        )
        fixplan = values.get("plan")
        if fixplan is None:
            return {"plan": {}}
        return {
            "plan": {
                "summary": fixplan.summary,
                "approach": fixplan.approach,
                "files": [{"path": f.path, "action": f.action} for f in fixplan.files],
            }
        }

    return asyncio.run(_run())
