import json
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ai.checkpointer import checkpointer
from ai.graphs.chat import build_chat_graph
from app.auth.dependencies import AuthedUser, get_current_user, verify_installation_access
from app.db.models import ChatThread, Installation, Repository
from app.db.session import get_session

router = APIRouter()

# Built once; each request compiles it against a per-request checkpointer so chat
# memory persists per thread_id without sharing one Postgres connection across requests.
_builder = build_chat_graph()


class ChatRequest(BaseModel):
    """Boundary shape for a chat turn; `thread_id` continues an existing
    conversation (omit it to start a new one)."""

    repo: str
    message: str
    thread_id: str | None = None


def _sse(data: dict) -> str:
    """Encode one Server-Sent Event frame as a JSON `data:` line."""
    return f"data: {json.dumps(data)}\n\n"


async def _installation_id(session: AsyncSession, repo: str) -> int:
    """Resolve a repo's GitHub installation id (used to mint file-read tokens);
    404 if the repo has no installation, i.e. the app was never installed on it."""
    result = await session.execute(
        select(Installation.github_installation_id)
        .join(Repository, Repository.installation_id == Installation.id)
        .where(Repository.full_name == repo)
    )
    installation_id = result.scalar_one_or_none()
    if installation_id is None:
        raise HTTPException(status_code=404, detail=f"repo not installed: {repo}")
    return installation_id


@router.post("/chat")
async def chat(
    req: ChatRequest,
    session: AsyncSession = Depends(get_session),
    authed: AuthedUser = Depends(get_current_user),
) -> StreamingResponse:
    """Stream a grounded, memory-backed answer over the repo's semantic index via
    SSE; the only synchronous AI path (invariant #2). Session-gated and access-checked
    (invariant #13): resolves the installation before streaming so a missing repo fails
    fast with 404 and an unauthorized one with 403, not mid-stream."""
    installation_id = await _installation_id(session, req.repo)
    await verify_installation_access(authed, installation_id)
    thread_id = req.thread_id or str(uuid.uuid4())

    if req.thread_id is None:
        session.add(ChatThread(
            thread_id=thread_id,
            user_id=authed.user.id,
            repo=req.repo,
            title=req.message[:80],
        ))
    else:
        result = await session.execute(
            select(ChatThread).where(ChatThread.thread_id == thread_id)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            # Pre-Phase-6 thread: no ownership row yet — claim it now so the
            # GET history endpoint starts working after this first turn.
            session.add(ChatThread(
                thread_id=thread_id,
                user_id=authed.user.id,
                repo=req.repo,
                title=req.message[:80],
            ))
        elif existing.user_id != authed.user.id:
            raise HTTPException(status_code=403, detail="no access to thread")
        else:
            existing.updated_at = datetime.now(timezone.utc)
    await session.commit()

    config = {
        "configurable": {
            "thread_id": thread_id,
            "repo": req.repo,
            "installation_id": installation_id,
        }
    }
    # Reset query/rewrites each turn so the checkpointed prior turn doesn't leak in.
    initial = {"messages": [HumanMessage(req.message)], "query": "", "rewrites": 0}

    async def stream() -> AsyncGenerator[str, None]:
        yield _sse({"thread_id": thread_id})
        async with checkpointer() as saver:
            graph = _builder.compile(checkpointer=saver)
            async for chunk, metadata in graph.astream(
                initial, config, stream_mode="messages"
            ):
                if metadata.get("langgraph_node") != "generate":
                    continue
                text = chunk.content
                if isinstance(text, str) and text:
                    yield _sse({"delta": text})
        yield _sse({"done": True})

    return StreamingResponse(stream(), media_type="text/event-stream")
