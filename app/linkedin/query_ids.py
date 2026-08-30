"""Resolving LinkedIn's GraphQL ``queryId`` hashes — the fragile part.

Every ``/voyager/api/graphql`` call must name a persisted query by an opaque id
such as ``voyagerIdentityDashProfileCards.4d0e5f...``. LinkedIn generates these
at build time and ships them inside its Ember JS bundles, and **they rotate
whenever LinkedIn deploys**. Hardcoding them is why most published LinkedIn
scrapers work for a few months and then quietly return empty results.

So resolution is a three-tier chain, tried in order:

1. **Operator override** — ``query_ids.json`` on disk / env. Last-known-good
   values an operator can correct without a redeploy.
2. **Runtime discovery** — fetch a LinkedIn page, pull the ``static.licdn.com``
   bundle URLs out of the HTML, fetch those bundles and regex the ids straight
   out of the JavaScript. This is what lets the service heal itself.
3. **Bundled defaults** — compiled-in constants as a floor.

When a request fails with :class:`QueryIdError`, the caller invalidates the
entry and re-runs discovery once before retrying. That loop is the difference
between a scraper with a three-month lifespan and one that keeps working.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from app.observability.logging import get_logger

if TYPE_CHECKING:
    from app.linkedin.client import VoyagerClient

logger = get_logger(__name__)

# Query names the profile pipeline depends on.
PROFILE_BY_VANITY = "voyagerIdentityDashProfiles"
PROFILE_CARDS = "voyagerIdentityDashProfileCards"
PROFILE_COMPONENTS = "voyagerIdentityDashProfileComponents"

#: The queries the profile pipeline needs resolved before it can call GraphQL.
REQUIRED_QUERIES: tuple[str, ...] = (PROFILE_BY_VANITY, PROFILE_CARDS, PROFILE_COMPONENTS)

#: Tier 3 floor, populated by ``scripts/recon.py`` once real ids have been
#: observed against a live session. Deliberately empty by default: a plausible
#: but wrong hash produces a confusing 400 from LinkedIn, whereas an absent one
#: fails loudly and routes straight to discovery. Never invent values here.
DEFAULT_QUERY_IDS: dict[str, str] = {}

#: Matches a persisted-query id literal inside a JS bundle.
_QUERY_ID_RE = re.compile(r"\b(voyager[A-Za-z0-9]+)\.([0-9a-f]{32})\b")

#: LinkedIn's static asset host, as referenced from page HTML.
_BUNDLE_RE = re.compile(
    r"https://static\.licdn\.com/(?:aero-v1|sc)/[^\"'\s>]+?\.js", re.IGNORECASE
)

#: Bundles whose names hint at profile/identity code, scanned first.
_PRIORITY_HINTS = ("profile", "identity", "entity", "graphql", "main", "chunk")


class QueryIdResolver:
    def __init__(
        self,
        *,
        path: Path | None = None,
        ttl_seconds: int = 21600,
        discovery_enabled: bool = True,
        max_bundles: int = 25,
    ) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._discovery_enabled = discovery_enabled
        self._max_bundles = max_bundles
        self._ids: dict[str, str] = dict(DEFAULT_QUERY_IDS)
        self._source: dict[str, str] = dict.fromkeys(DEFAULT_QUERY_IDS, "default")
        self._discovered_at: float = 0.0
        self._lock = asyncio.Lock()
        self._load_overrides()

    # ------------------------------------------------------- tier 1: on disk

    def _load_overrides(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("query_ids.override_unreadable", path=str(self._path), error=str(exc))
            return
        if not isinstance(data, dict):
            logger.warning("query_ids.override_not_an_object", path=str(self._path))
            return
        applied = 0
        for name, value in data.items():
            if isinstance(value, str) and value:
                self._ids[name] = value
                self._source[name] = "override"
                applied += 1
        if applied:
            logger.info("query_ids.overrides_loaded", path=str(self._path), count=applied)

    def _persist(self) -> None:
        """Write discovered ids back to disk so a restart starts warm."""
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._ids, indent=2, sort_keys=True) + "\n")
        except OSError as exc:  # read-only filesystem is fine, just noisy
            logger.debug("query_ids.persist_failed", error=str(exc))

    # ------------------------------------------------------------ public API

    def get(self, name: str) -> str:
        return self._ids.get(name, DEFAULT_QUERY_IDS.get(name, ""))

    def source_of(self, name: str) -> str:
        return self._source.get(name, "unknown")

    @property
    def missing(self) -> list[str]:
        """Required queries with no id yet — GraphQL cannot run without these."""
        return [name for name in REQUIRED_QUERIES if not self._ids.get(name)]

    @property
    def is_usable(self) -> bool:
        """True when every required query id is known.

        The GraphQL tier checks this and steps aside for the REST/HTML tiers
        rather than sending a request that is guaranteed to 400.
        """
        return not self.missing

    @property
    def is_stale(self) -> bool:
        return (time.monotonic() - self._discovered_at) > self._ttl

    def invalidate(self, name: str | None = None) -> None:
        """Force the next :meth:`ensure_fresh` to re-discover."""
        self._discovered_at = 0.0
        logger.info("query_ids.invalidated", name=name or "all")

    async def ensure_fresh(self, client: VoyagerClient, *, force: bool = False) -> None:
        if not self._discovery_enabled:
            return
        # Missing ids always warrant a scan, however recently one ran.
        needed = force or self.is_stale or bool(self.missing)
        if not needed:
            return
        async with self._lock:
            # Another coroutine may have refreshed while we waited on the lock.
            if not (force or self.is_stale or self.missing):
                return
            try:
                await self._discover(client)
            except Exception as exc:  # discovery is best-effort by design
                logger.warning("query_ids.discovery_failed", error=str(exc))
                # Back off so a hard failure does not re-scan on every request.
                self._discovered_at = time.monotonic() - self._ttl + 300

    # ---------------------------------------------------- tier 2: discovery

    async def _discover(self, client: VoyagerClient) -> None:
        started = time.monotonic()
        html = await client.get_html("https://www.linkedin.com/feed/")
        bundles = self._rank_bundles(_BUNDLE_RE.findall(html))
        if not bundles:
            logger.warning("query_ids.no_bundles_found")
            return

        found: dict[str, str] = {}
        # Scan in small concurrent batches, stopping as soon as every needed id
        # is accounted for — bundles are large and there are hundreds of them.
        for batch_start in range(0, min(len(bundles), self._max_bundles), 5):
            batch = bundles[batch_start : batch_start + 5]
            results = await asyncio.gather(
                *(self._scan_bundle(client, url) for url in batch),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, dict):
                    found.update(result)
            if all(name in found for name in REQUIRED_QUERIES):
                break

        if not found:
            logger.warning("query_ids.discovery_empty", bundles_scanned=len(bundles))
            return

        changed = []
        for name, full_id in found.items():
            if self._source.get(name) == "override":
                continue  # an explicit operator override always wins
            if self._ids.get(name) != full_id:
                changed.append(name)
            self._ids[name] = full_id
            self._source[name] = "discovered"

        self._discovered_at = time.monotonic()
        self._persist()
        logger.info(
            "query_ids.discovered",
            found=len(found),
            changed=changed,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def _rank_bundles(self, urls: list[str]) -> list[str]:
        """Deduplicate and float likely-relevant bundles to the front."""
        seen: dict[str, None] = {}
        for u in urls:
            seen.setdefault(u, None)
        unique = list(seen)

        def score(url: str) -> int:
            low = url.lower()
            return -sum(2 if h in low else 0 for h in _PRIORITY_HINTS)

        return sorted(unique, key=score)

    async def _scan_bundle(self, client: VoyagerClient, url: str) -> dict[str, str]:
        try:
            body = await client.get_raw(url)
        except Exception as exc:
            logger.debug("query_ids.bundle_fetch_failed", url=url[:120], error=str(exc))
            return {}
        out: dict[str, str] = {}
        for name, digest in _QUERY_ID_RE.findall(body):
            if name in REQUIRED_QUERIES:
                out[name] = f"{name}.{digest}"
        return out

    # --------------------------------------------------------------- status

    def snapshot(self) -> dict[str, object]:
        return {
            "discovery_enabled": self._discovery_enabled,
            "usable": self.is_usable,
            "missing": self.missing,
            "stale": self.is_stale,
            "age_seconds": (
                round(time.monotonic() - self._discovered_at) if self._discovered_at else None
            ),
            "ids": {
                name: {"id": value, "source": self._source.get(name, "unknown")}
                for name, value in self._ids.items()
            },
        }
