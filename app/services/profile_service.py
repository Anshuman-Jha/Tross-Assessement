"""Application service: cache → fetch → shape the response envelope.

Also collapses concurrent requests for the *same* profile into one upstream
fetch. Without that, three simultaneous requests for a popular profile become
three full section-fan-outs against LinkedIn — the exact traffic pattern that
gets an account throttled.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from app.linkedin.fetcher import ProfileFetcher
from app.linkedin.profile_url import canonical_profile_url, extract_public_identifier
from app.models.profile import Profile, ProfileResponse, ResponseMeta, Source
from app.observability.logging import get_logger
from app.services.cache import Cache

logger = get_logger(__name__)

CACHE_VERSION = "v1"


class ProfileService:
    def __init__(self, fetcher: ProfileFetcher, cache: Cache, *, ttl: int = 3600) -> None:
        self._fetcher = fetcher
        self._cache = cache
        self._ttl = ttl
        #: One in-flight fetch per public id, shared by all waiters.
        self._inflight: dict[str, asyncio.Task[ProfileResponse]] = {}

    async def get_profile(self, raw_url: str, *, refresh: bool = False) -> ProfileResponse:
        started = time.monotonic()
        public_id = extract_public_identifier(raw_url)
        cache_key = f"{CACHE_VERSION}:{public_id.lower()}"

        if not refresh:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                logger.info("profile.cache_hit", public_id=public_id)
                response = ProfileResponse.model_validate(cached)
                response.meta.cached = True
                response.meta.duration_ms = round((time.monotonic() - started) * 1000)
                return response

        return await self._fetch_once(public_id, cache_key, started)

    async def _fetch_once(
        self, public_id: str, cache_key: str, started: float
    ) -> ProfileResponse:
        """Run one upstream fetch per profile, however many callers are waiting."""
        existing = self._inflight.get(cache_key)
        if existing is not None:
            logger.info("profile.coalesced", public_id=public_id)
            response = await asyncio.shield(existing)
            # Each waiter reports its own latency.
            return response.model_copy(
                update={
                    "meta": response.meta.model_copy(
                        update={"duration_ms": round((time.monotonic() - started) * 1000)}
                    )
                }
            )

        task = asyncio.create_task(self._do_fetch(public_id, cache_key, started))
        self._inflight[cache_key] = task
        try:
            return await task
        finally:
            self._inflight.pop(cache_key, None)

    async def _do_fetch(
        self, public_id: str, cache_key: str, started: float
    ) -> ProfileResponse:
        logger.info("profile.fetch_start", public_id=public_id)
        result = await self._fetcher.fetch(public_id)

        response = ProfileResponse(
            success=True,
            meta=ResponseMeta(
                profile_url=canonical_profile_url(public_id),
                public_identifier=public_id,
                profile_urn=result.profile_urn,
                fetched_at=datetime.now(UTC),
                source=result.source,
                cached=False,
                duration_ms=round((time.monotonic() - started) * 1000),
                completeness=result.completeness,
            ),
            profile=result.profile,
            warnings=result.warnings,
        )

        await self._cache.set(cache_key, response.model_dump(mode="json"), self._ttl)
        logger.info(
            "profile.fetch_complete",
            public_id=public_id,
            source=result.source.value,
            duration_ms=response.meta.duration_ms,
            warnings=len(result.warnings),
        )
        return response

    async def invalidate(self, raw_url: str) -> None:
        public_id = extract_public_identifier(raw_url)
        await self._cache.delete(f"{CACHE_VERSION}:{public_id.lower()}")


def empty_profile() -> Profile:
    """A blank profile, used when a tier yields structure but no content."""
    return Profile()


__all__ = ["CACHE_VERSION", "ProfileService", "Source", "empty_profile"]
