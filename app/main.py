from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ai.checkpointer import setup_checkpointer
from app.api import router as api_router
from app.chat import router as chat_router
from app.config import settings
from app.db.session import create_db
from app.github.webhooks import router as webhooks_router
from app.observability import configure_langsmith


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_langsmith()
    await create_db()
    await setup_checkpointer()
    yield


app = FastAPI(title="Revet | AI Code Review Assistant", lifespan=lifespan)

# CORS for the credentialed web frontend (Phase 5); configured once, not per-handler.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhooks_router)
app.include_router(chat_router)
app.include_router(api_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
