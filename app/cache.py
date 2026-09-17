"""Cache adapters implementing the ``Cache`` port (see app.domain.ports).

RedisCache is the production implementation; InMemoryCache serves unit tests.
Every Redis operation degrades gracefully: when Redis is unreachable the call
is logged as ``redis_unavailable`` and the caller gets cache-miss semantics,
so the API keeps working straight from PostgreSQL / the provider.
"""

import time
from functools import lru_cache
from typing import Any

import orjson
import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("cache")


class RedisCache:
    """Redis-backed JSON cache with fail-open error handling."""

    def __init__(self, redis_url: str) -> None:
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    async def get_json(self, key: str) -> dict | list | None:
        """Return the decoded value for ``key`` or None on miss/error."""
        try:
            raw = await self._redis.get(key)
        except Exception as exc:
            log.warning("redis_unavailable", operation="get", key=key, error=str(exc))
            return None
        if raw is None:
            return None
        return orjson.loads(raw)

    async def set_json(self, key: str, value: dict | list, ttl_seconds: int) -> None:
        """Store ``value`` as JSON with a TTL; no-op on Redis failure."""
        try:
            await self._redis.set(key, orjson.dumps(value), ex=ttl_seconds)
        except Exception as exc:
            log.warning("redis_unavailable", operation="set", key=key, error=str(exc))

    async def delete(self, key: str) -> None:
        """Drop ``key``; no-op on Redis failure."""
        try:
            await self._redis.delete(key)
        except Exception as exc:
            log.warning("redis_unavailable", operation="delete", key=key, error=str(exc))


class InMemoryCache:
    """Dict-based cache with lazy TTL checks — for tests and local runs."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}

    async def get_json(self, key: str) -> dict | list | None:
        item = self._store.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at <= time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    async def set_json(self, key: str, value: dict | list, ttl_seconds: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl_seconds)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


@lru_cache(maxsize=1)
def get_cache() -> RedisCache:
    """Process-wide cache instance (tests may inject InMemoryCache instead)."""
    return RedisCache(get_settings().redis_url)
