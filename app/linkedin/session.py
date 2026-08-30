"""A single authenticated LinkedIn session and its health state.

LinkedIn's CSRF scheme is unusual and worth stating plainly, because it is the
detail most reimplementations get wrong: the `JSESSIONID` cookie value *is* the
CSRF token. LinkedIn sets it with literal double quotes around it
(`JSESSIONID="ajax:1234567890"`), the cookie must be sent with the quotes, and
the `csrf-token` header must carry the same value with the quotes stripped.
Mismatch that and every write-shaped request comes back 403.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.observability.logging import get_logger

logger = get_logger(__name__)


class SessionState(StrEnum):
    HEALTHY = "healthy"
    #: Temporarily rested after a rate limit; returns to healthy automatically.
    COOLING_DOWN = "cooling_down"
    #: Cookie rejected by LinkedIn. Needs human intervention to fix.
    DEAD = "dead"


@dataclass
class LinkedInSession:
    """Credentials plus the bookkeeping needed to rotate and rest them."""

    li_at: str
    jsessionid: str = ""
    label: str = "default"

    #: Cookies LinkedIn hands us that must be echoed back on later requests.
    #: `lidc` pins the session to a datacenter; omitting it makes LinkedIn
    #: redirect to the same URL trying to set it, which reads as a redirect
    #: loop. `bcookie`/`bscookie` are browser-identity cookies a real client
    #: always carries.
    extra_cookies: dict[str, str] = field(default_factory=dict)

    state: SessionState = SessionState.HEALTHY
    cooldown_until: float = 0.0
    last_used_at: float = 0.0
    request_count: int = 0
    error_count: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    #: Timestamps of recent requests, for the per-session token bucket.
    _recent_requests: list[float] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ auth

    @property
    def csrf_token(self) -> str:
        """The `csrf-token` header value: JSESSIONID without its literal quotes."""
        return self.jsessionid.strip().strip('"')

    @property
    def has_jsessionid(self) -> bool:
        return bool(self.csrf_token)

    def cookie_header(self) -> str:
        """Serialise cookies exactly as a browser would send them.

        JSESSIONID keeps its quotes here; only the header form drops them.
        """
        parts = [f"li_at={self.li_at}"]
        if self.has_jsessionid:
            token = self.csrf_token
            parts.append(f'JSESSIONID="{token}"')
        parts.extend(f"{name}={value}" for name, value in self.extra_cookies.items())
        return "; ".join(parts)

    def absorb(self, cookies: dict[str, str]) -> None:
        """Store cookies LinkedIn set, so later requests echo them back.

        `li_at` is deliberately never overwritten: the operator's configured
        value stays authoritative, and LinkedIn echoing it back (or clearing
        it) must not silently replace it.
        """
        for raw_name, raw_value in cookies.items():
            name = raw_name.strip()
            value = raw_value.strip()
            lowered = name.lower()

            if lowered == "li_at":
                continue
            if lowered == "jsessionid":
                self.adopt_jsessionid(value)
                continue
            if not value:
                # An empty value is a deletion.
                self.extra_cookies.pop(name, None)
                continue
            self.extra_cookies[name] = value

    def adopt_jsessionid(self, value: str) -> None:
        """Accept a JSESSIONID handed to us by LinkedIn via Set-Cookie.

        Lets an operator configure only `li_at` and have the rest bootstrap
        itself on first use, which removes a whole class of setup mistakes —
        including the CSRF mismatch you get from pasting a `JSESSIONID` that
        belongs to a different login session than the `li_at`.
        """
        cleaned = value.strip().strip('"')
        if cleaned and cleaned != self.csrf_token:
            self.jsessionid = cleaned
            logger.info("session.jsessionid_bootstrapped", session=self.label)

    # ---------------------------------------------------------------- health

    @property
    def is_available(self) -> bool:
        if self.state is SessionState.DEAD:
            return False
        if self.state is SessionState.COOLING_DOWN:
            if time.monotonic() >= self.cooldown_until:
                self.state = SessionState.HEALTHY
                self.consecutive_errors = 0
                logger.info("session.cooldown_expired", session=self.label)
                return True
            return False
        return True

    def cool_down(self, seconds: float, reason: str) -> None:
        self.state = SessionState.COOLING_DOWN
        self.cooldown_until = time.monotonic() + seconds
        self.last_error = reason
        logger.warning(
            "session.cooling_down", session=self.label, seconds=round(seconds, 1), reason=reason
        )

    def mark_dead(self, reason: str) -> None:
        self.state = SessionState.DEAD
        self.last_error = reason
        logger.error("session.dead", session=self.label, reason=reason)

    def record_success(self) -> None:
        self.request_count += 1
        self.consecutive_errors = 0
        self.last_used_at = time.monotonic()
        self._recent_requests.append(self.last_used_at)

    def record_error(self, reason: str) -> None:
        self.error_count += 1
        self.consecutive_errors += 1
        self.last_error = reason
        self.last_used_at = time.monotonic()
        self._recent_requests.append(self.last_used_at)

    # ----------------------------------------------------------- rate limits

    def requests_in_last_minute(self) -> int:
        cutoff = time.monotonic() - 60.0
        self._recent_requests = [t for t in self._recent_requests if t >= cutoff]
        return len(self._recent_requests)

    def snapshot(self) -> dict[str, object]:
        """Health view for `/readyz`. Deliberately contains no credentials."""
        return {
            "label": self.label,
            "state": self.state.value,
            "requests": self.request_count,
            "errors": self.error_count,
            "consecutive_errors": self.consecutive_errors,
            "requests_last_minute": self.requests_in_last_minute(),
            "has_csrf": self.has_jsessionid,
            "cooldown_remaining_s": max(0, round(self.cooldown_until - time.monotonic()))
            if self.state is SessionState.COOLING_DOWN
            else 0,
            "last_error": self.last_error,
        }
