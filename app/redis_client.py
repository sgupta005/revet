import asyncio

from redis.asyncio import Redis

from app.config import settings

_redis: Redis | None = None
_redis_loop: asyncio.AbstractEventLoop | None = None


def get_redis() -> Redis:
    """Return a Redis client bound to the *current* running event loop.

    A ``redis.asyncio`` client binds its connection pool to the event loop it
    first runs a command on. FastAPI serves every request on one long-lived
    loop, so a single process-wide client is correct there. But Celery prefork
    tasks each run under their own ``asyncio.run()`` loop (invariant #3): reusing
    a client created on a previous, now-closed loop makes the *first* command on
    the new loop raise "Event loop is closed". The pool then evicts that dead
    connection, which is exactly why the Celery retry silently succeeds while the
    first attempt fails. Caching the client per running loop and rebuilding when
    the loop changes gives every task loop its own client, so the first run
    succeeds without relying on the retry.

    Must be called from within a running event loop (all callers are ``async``).
    """
    global _redis, _redis_loop
    loop = asyncio.get_running_loop()
    if _redis is None or _redis_loop is not loop:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
        _redis_loop = loop
    return _redis


async def close_redis() -> None:
    """Close and forget the current client. Celery task entrypoints call this in
    their ``finally`` so the loop's redis connections are released before the loop
    closes — mirroring how the per-run engine/store are disposed (invariant #3)."""
    global _redis, _redis_loop
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        _redis_loop = None
