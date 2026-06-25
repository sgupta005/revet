from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

import app.db.models  # noqa: F401 — registers all models before create_all
from app.config import settings

_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def build_engine() -> AsyncEngine:
    # Celery prefork tasks call asyncio.run() per invocation, so each task builds
    # (and disposes) its own engine rather than reusing the module-level one,
    # which would bind connections to an already-closed event loop.
    return create_async_engine(_url, echo=False)


async def create_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
