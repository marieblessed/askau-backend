"""Redis access with a single rule: a cache failure never fails a request.

Every method degrades to a miss. The alternative — propagating Redis errors —
turns a cache outage into a total outage, which is a worse failure than a slow
one.
"""

from __future__ import annotations

from redis.asyncio import Redis

from askau.settings import Settings

_client: Redis | None = None


def get_redis(settings: Settings) -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def ping(settings: Settings) -> bool:
    try:
        return bool(await get_redis(settings).ping())
    except Exception:
        return False
