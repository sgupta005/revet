import time
from datetime import datetime, timezone

import httpx
import jwt

from app.config import settings
from app.github.constants import GITHUB_API, INST_TOKEN_KEY, INST_TOKEN_TTL_BUFFER
from app.redis_client import get_redis


def _app_jwt() -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": settings.github_app_id}
    key = settings.github_app_private_key.replace("\\n", "\n")
    return jwt.encode(payload, key, algorithm="RS256")


async def get_installation_token(installation_id: int) -> str:
    redis = get_redis()
    key = INST_TOKEN_KEY.format(installation_id=installation_id)
    cached = await redis.get(key)
    if cached:
        return cached

    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=10) as client:
        resp = await client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {_app_jwt()}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    token: str = data["token"]
    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds()) - INST_TOKEN_TTL_BUFFER
    if ttl > 0:
        await redis.set(key, token, ex=ttl)
    return token
