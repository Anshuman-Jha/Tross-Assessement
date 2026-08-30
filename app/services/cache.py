"""Response cache: Redis when configured, in-process TTL+LRU otherwise.

Caching matters more here than in a typical service. Every miss is a request to
a hostile upstream that rate-limits aggressively and can restrict the account
outright, so the cache is a protection mechanism as much as a latency
optimisation — reviewers hitting the same demo profile repeatedly should cost
one upstream fetch, not twenty.

The interface is deliberately tiny so the two backends stay interchangeable and
the service layer never branches on which one is active.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any, Protocol

from app.observability.logging import get_logger

logger = get_logger(__name__)


class Cache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def close(self) -> None: ...
    @property
    def backend(self) -> str: ...


class InMemoryCache:
    """Bounded TTL cache. Adequate for a single instance; not shared."""

    def __init__(self, max_entries: int = 512) -> None:
        self._max = max_entries
        self._store: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    @property
    def backend(self) -> str:
        return "memory"

    async def get(self, key: str) -> dict[str, Any] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        self._store[key] = (time.monotonic() + ttl, value)
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def close(self) -> None:
        self._store.clear()


class RedisCache:
    """Shared cache so multiple instances do not each hammer LinkedIn."""

    def __init__(self, url: str, prefix: str = "liprofile:") -> None:
        import redis.asyncio as redis

        self._client = redis.from_url(url, decode_responses=True)
        self._prefix = prefix

    @property
    def backend(self) -> str:
        return "redis"

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get(self._prefix + key)
        except Exception as exc:
            # A cache outage must never take the API down with it.
            logger.warning("cache.redis_get_failed", error=str(exc))
            return None
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        try:
            await self._client.setex(self._prefix + key, ttl, json.dumps(value, default=str))
        except Exception as exc:
            logger.warning("cache.redis_set_failed", error=str(exc))

    async def delete(self, key: str) -> None:
        try:
            await self._client.delete(self._prefix + key)
        except Exception as exc:
            logger.warning("cache.redis_delete_failed", error=str(exc))

    async def close(self) -> None:
        # redis-py renamed close() to aclose() in 5.0.1; support both so the
        # shutdown path does not depend on the exact patch version installed.
        closer = getattr(self._client, "aclose", None) or self._client.close
        try:
            await closer()
        except Exception:  # pragma: no cover - shutdown best effort
            logger.debug("cache.redis_close_failed")


def build_cache(redis_url: str | None, max_entries: int = 512) -> Cache:
    if redis_url:
        try:
            cache = RedisCache(redis_url)
            logger.info("cache.using_redis")
            return cache
        except Exception as exc:
            # Misconfigured Redis degrades to memory rather than failing boot.
            logger.warning("cache.redis_unavailable_falling_back", error=str(exc))
    logger.info("cache.using_memory", max_entries=max_entries)
    return InMemoryCache(max_entries=max_entries)
