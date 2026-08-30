"""Rotates a set of LinkedIn sessions and keeps unhealthy ones out of service.

Selection is least-recently-used rather than round-robin: LRU naturally spreads
load when sessions drop in and out of cooldown, whereas a round-robin cursor
tends to re-hit whichever session just recovered.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from app.linkedin.exceptions import NoHealthySessionError
from app.linkedin.session import LinkedInSession, SessionState
from app.observability.logging import get_logger

logger = get_logger(__name__)


class SessionPool:
    def __init__(
        self,
        sessions: Sequence[LinkedInSession],
        *,
        requests_per_minute: int = 30,
        cooldown_seconds: int = 900,
        max_consecutive_errors: int = 5,
    ) -> None:
        self._sessions = list(sessions)
        self._requests_per_minute = requests_per_minute
        self._cooldown_seconds = cooldown_seconds
        self._max_consecutive_errors = max_consecutive_errors
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def sessions(self) -> list[LinkedInSession]:
        return list(self._sessions)

    def healthy_count(self) -> int:
        return sum(1 for s in self._sessions if s.is_available)

    # ------------------------------------------------------------ selection

    async def acquire(self) -> LinkedInSession:
        """Pick the least-recently-used session that is healthy and under quota."""
        async with self._lock:
            if not self._sessions:
                raise NoHealthySessionError(
                    "No LinkedIn credentials are configured. Set LINKEDIN_LI_AT "
                    "(and optionally LINKEDIN_JSESSIONID) in the environment."
                )

            available = [s for s in self._sessions if s.is_available]
            if not available:
                raise NoHealthySessionError(
                    self._describe_unavailable(),
                    detail={"sessions": [s.snapshot() for s in self._sessions]},
                )

            # Prefer sessions still under their per-minute budget.
            under_quota = [
                s for s in available if s.requests_in_last_minute() < self._requests_per_minute
            ]
            pool = under_quota or available
            chosen = min(pool, key=lambda s: s.last_used_at)

            if not under_quota:
                logger.warning(
                    "session_pool.all_at_quota",
                    rpm_limit=self._requests_per_minute,
                    sessions=len(available),
                )
            chosen.last_used_at = time.monotonic()
            return chosen

    def _describe_unavailable(self) -> str:
        dead = sum(1 for s in self._sessions if s.state is SessionState.DEAD)
        cooling = sum(1 for s in self._sessions if s.state is SessionState.COOLING_DOWN)
        parts = []
        if dead:
            parts.append(f"{dead} session(s) rejected by LinkedIn (cookie expired or revoked)")
        if cooling:
            soonest = min(
                (s.cooldown_until for s in self._sessions if s.state is SessionState.COOLING_DOWN),
                default=0.0,
            )
            wait = max(0, round(soonest - time.monotonic()))
            parts.append(f"{cooling} session(s) cooling down for another ~{wait}s")
        return "No LinkedIn session is currently usable: " + "; ".join(parts) + "."

    # --------------------------------------------------------- health hooks

    def report_success(self, session: LinkedInSession) -> None:
        session.record_success()

    def report_error(
        self, session: LinkedInSession, reason: str, *, fatal: bool = False, cooldown: float | None
        = None
    ) -> None:
        """Record a failure and quarantine the session if warranted.

        `fatal` means LinkedIn rejected the credential itself (401/challenge) —
        retrying cannot help and only accelerates a restriction, so the session
        is removed from service until an operator replaces the cookie.
        """
        session.record_error(reason)
        if fatal:
            session.mark_dead(reason)
            return
        if cooldown is not None:
            session.cool_down(cooldown, reason)
            return
        if session.consecutive_errors >= self._max_consecutive_errors:
            session.cool_down(
                self._cooldown_seconds,
                f"{session.consecutive_errors} consecutive errors; last: {reason}",
            )

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[LinkedInSession]:
        """Acquire a session; success is recorded unless the body raises."""
        session = await self.acquire()
        try:
            yield session
        except Exception:
            raise
        else:
            self.report_success(session)

    def snapshot(self) -> dict[str, object]:
        return {
            "total": len(self._sessions),
            "healthy": self.healthy_count(),
            "sessions": [s.snapshot() for s in self._sessions],
        }


def build_pool_from_settings(accounts: Sequence[dict[str, str]], **kwargs: object) -> SessionPool:
    sessions = [
        LinkedInSession(
            li_at=a["li_at"],
            jsessionid=a.get("jsessionid", ""),
            label=a.get("label", f"account-{i}"),
        )
        for i, a in enumerate(accounts)
    ]
    return SessionPool(sessions, **kwargs)  # type: ignore[arg-type]
