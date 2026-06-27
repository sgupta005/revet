import json
import secrets
from dataclasses import asdict, dataclass

from app.config import settings
from app.github.constants import SESSION_KEY
from app.github.oauth import OAuthTokens
from app.redis_client import get_redis


@dataclass
class SessionData:
    """Server-side session contents. The user/refresh tokens live only here (Redis),
    never in the browser — the browser holds only the opaque `session_token`."""

    user_id: int
    user_token: str
    refresh_token: str


async def create_session(user_id: int, tokens: OAuthTokens) -> str:
    """Create an opaque session and return its token; TTL is `settings.session_ttl`."""
    session_token = secrets.token_urlsafe(32)
    await _write(session_token, SessionData(user_id, tokens.access_token, tokens.refresh_token))
    return session_token


async def get_session(session_token: str) -> SessionData | None:
    """Resolve a session token to its data, or None if missing/expired."""
    raw = await get_redis().get(SESSION_KEY.format(session_token=session_token))
    if raw is None:
        return None
    return SessionData(**json.loads(raw))


async def update_session_tokens(session_token: str, tokens: OAuthTokens) -> None:
    """Persist refreshed user tokens (slides the session TTL); no-op if the session
    is already gone."""
    data = await get_session(session_token)
    if data is None:
        return
    data.user_token = tokens.access_token
    if tokens.refresh_token:
        data.refresh_token = tokens.refresh_token
    await _write(session_token, data)


async def delete_session(session_token: str) -> None:
    """Invalidate a session (logout)."""
    await get_redis().delete(SESSION_KEY.format(session_token=session_token))


async def _write(session_token: str, data: SessionData) -> None:
    await get_redis().set(
        SESSION_KEY.format(session_token=session_token),
        json.dumps(asdict(data)),
        ex=settings.session_ttl,
    )
