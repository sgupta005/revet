from contextlib import AbstractAsyncContextManager

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import settings


def _conn_string() -> str:
    """Strip any SQLAlchemy +driver suffix since langgraph
    does not use SQLAlchemy."""
    return (
        settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        .replace("postgresql+psycopg://", "postgresql://", 1)
    )


def checkpointer() -> AbstractAsyncContextManager[AsyncPostgresSaver]:
    """Return an AsyncPostgresSaver async context manager (chat memory keyed by
    thread_id). Caller enters the context inside the task's
    event loop and runs `await saver.setup()` once before first use."""
    return AsyncPostgresSaver.from_conn_string(_conn_string())


async def setup_checkpointer() -> None:
    """Create the LangGraph checkpoint tables once at startup; `setup()` is
    idempotent (CREATE TABLE IF NOT EXISTS) so per-request savers can skip it."""
    async with checkpointer() as saver:
        await saver.setup()


async def get_thread_messages(thread_id: str) -> list[dict[str, str]]:
    """Read [{role, content}] for a thread from the LangGraph checkpointer.
    Filters to human/ai messages only; skips tool-call chunks where content is
    not a plain string. Returns an empty list when no checkpoint exists yet."""
    config: dict = {"configurable": {"thread_id": thread_id}}
    async with checkpointer() as saver:
        tup = await saver.aget_tuple(config)
    if tup is None:
        return []
    channel_values = tup.checkpoint.get("channel_values", {})
    messages = channel_values.get("messages", [])
    result: list[dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, (HumanMessage, AIMessage)):
            continue
        if not isinstance(msg.content, str) or not msg.content:
            continue
        role = "user" if isinstance(msg, HumanMessage) else "assistant"
        result.append({"role": role, "content": msg.content})
    return result
