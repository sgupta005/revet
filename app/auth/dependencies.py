import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import get_session, update_session_tokens
from app.db.models import User
from app.db.session import get_session as get_db
from app.github.constants import USER_INSTALLATIONS_KEY, USER_CACHE_TTL
from app.github.oauth import (
    GitHubInstallation,
    list_user_installations,
    refresh_user_token,
)
from app.redis_client import get_redis

T = TypeVar("T")


@dataclass
class AuthedUser:
    """The resolved session: durable `User` plus the live user token used only for
    identity/access decisions. Token fields are mutated in place when a 401 forces
    a refresh, so a single request reuses the fresh token."""

    user: User
    session_token: str
    user_token: str
    refresh_token: str


def _extract_session_token(request: Request, authorization: str | None) -> str | None:
    """Read the opaque session token from the first-party `session` cookie or an
    `Authorization: Bearer` header (the frontend forwards it as one of the two)."""
    cookie = request.cookies.get("session")
    if cookie:
        return cookie
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ")
    return None


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> AuthedUser:
    """Resolve the session token → Redis session → `User`; 401 if missing/invalid.
    The single auth gate for every user-facing route (code-standards §auth)."""
    token = _extract_session_token(request, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing session")
    data = await get_session(token)
    if data is None:
        raise HTTPException(status_code=401, detail="invalid session")
    user = await db.get(User, data.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid session")
    return AuthedUser(user, token, data.user_token, data.refresh_token)


async def call_with_refresh(
    authed: AuthedUser, fn: Callable[[str], Awaitable[T]]
) -> T:
    """Run a user-token GitHub call, refreshing the token once on a 401 and
    persisting the new tokens to the session. Falls back to surfacing the 401 (as
    a re-login) when there is no refresh token (non-expiring-token mode)."""
    try:
        return await fn(authed.user_token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401 or not authed.refresh_token:
            raise
        tokens = await refresh_user_token(authed.refresh_token)
        await update_session_tokens(authed.session_token, tokens)
        authed.user_token = tokens.access_token
        if tokens.refresh_token:
            authed.refresh_token = tokens.refresh_token
        return await fn(authed.user_token)


async def user_installations(authed: AuthedUser) -> list[GitHubInstallation]:
    """The user's installations, briefly cached in Redis (access-check source of
    truth). Used by `/me` and every installation access check."""
    redis = get_redis()
    key = USER_INSTALLATIONS_KEY.format(user_id=authed.user.id)
    cached = await redis.get(key)
    if cached:
        return [GitHubInstallation(**item) for item in json.loads(cached)]
    installations = await call_with_refresh(authed, list_user_installations)
    await redis.set(
        key,
        json.dumps([i.model_dump() for i in installations]),
        ex=USER_CACHE_TTL,
    )
    return installations


async def verify_installation_access(authed: AuthedUser, installation_id: int) -> None:
    """403 unless the user can access the GitHub installation; `installation_id` is
    never trusted as a bare capability (invariant #13)."""
    installations = await user_installations(authed)
    if not any(i.id == installation_id for i in installations):
        raise HTTPException(status_code=403, detail="no access to installation")
