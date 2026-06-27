from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI

from ai.checkpointer import setup_checkpointer
from app.chat import router as chat_router
from app.db.session import create_db
from app.github.webhooks import router as webhooks_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await create_db()
    await setup_checkpointer()
    yield


app = FastAPI(title="Revet | AI Code Review Assistant", lifespan=lifespan)
app.include_router(webhooks_router)
app.include_router(chat_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
