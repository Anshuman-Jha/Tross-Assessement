"""Request dependencies: API-key auth and per-caller rate limiting.

The deployed service holds working LinkedIn credentials, so an open endpoint is
an open proxy onto someone's LinkedIn account. Both guards exist to protect the
account behind the API, not the API itself.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, Header, Request, status
from fastapi.responses import JSONResponse  # noqa: F401  (referenced in docs)

from app.config import Settings, get_settings
from app.linkedin.exceptions import LinkedInError
from app.observability.logging import get_logger

logger = get_logger(__name__)


class UnauthorizedError(LinkedInError):
    code = "UNAUTHORIZED"
    status_code = status.HTTP_401_UNAUTHORIZED


class TooManyRequestsError(LinkedInError):
    code = "TOO_MANY_REQUESTS"
    status_code = status.HTTP_429_TOO_MANY_REQUESTS

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> str | None:
    """Validate ``X-API-Key`` when the service is configured to require one."""
    if not settings.require_api_key:
        return None

    if not settings.api_keys:
        raise UnauthorizedError(
            "This deployment requires an API key but none are configured. "
            "Set API_KEYS, or set REQUIRE_API_KEY=false for local use."
        )
    if not x_api_key:
        raise UnauthorizedError("Missing X-API-Key header.")

    # Constant-time comparison so a timing side channel cannot reveal the key.
    for candidate in settings.api_keys:
        if secrets.compare_digest(x_api_key, candidate):
            return x_api_key

    logger.warning("api.invalid_key")
    raise UnauthorizedError("Invalid API key.")


class SlidingWindowLimiter:
    """Per-caller request cap over a sliding window.

    In-process, so it is per-instance rather than global; that is the right
    trade for a single-instance deployment and is documented as a limitation.
    """

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, identity: str) -> None:
        now = time.monotonic()
        bucket = self._hits[identity]
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self._max:
            retry_after = max(0.0, self._window - (now - bucket[0]))
            raise TooManyRequestsError(
                f"Rate limit exceeded: {self._max} requests per "
                f"{int(self._window)}s. Retry in {retry_after:.0f}s.",
                retry_after=retry_after,
            )
        bucket.append(now)

        # Opportunistic cleanup so idle callers do not accumulate forever.
        if len(self._hits) > 4096:
            for key in [k for k, v in self._hits.items() if not v]:
                self._hits.pop(key, None)


_limiter = SlidingWindowLimiter()


def configure_limiter(max_requests: int, window_seconds: float = 60.0) -> None:
    global _limiter
    _limiter = SlidingWindowLimiter(max_requests, window_seconds)


async def rate_limit(
    request: Request,
    api_key: str | None = Depends(require_api_key),
) -> None:
    """Throttle by API key, falling back to client IP when unauthenticated."""
    identity = api_key or (request.client.host if request.client else "unknown")
    _limiter.check(identity)
