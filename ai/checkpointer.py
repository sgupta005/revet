from contextlib import AbstractAsyncContextManager

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
