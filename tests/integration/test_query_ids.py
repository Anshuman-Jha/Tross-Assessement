"""The queryId resolver — the self-healing mechanism.

LinkedIn rotates these hashes on deploy, which is the single most common reason
a LinkedIn scraper stops working. The recovery path is therefore tested
directly: discover from live bundles, respect operator overrides, and
re-discover after a rejection.
"""

from __future__ import annotations

import json

import httpx
import respx

from app.linkedin.query_ids import (
    PROFILE_BY_VANITY,
    PROFILE_CARDS,
    PROFILE_COMPONENTS,
    QueryIdResolver,
)

FEED = "https://www.linkedin.com/feed/"
BUNDLE = "https://static.licdn.com/aero-v1/sc/h/profile-abc123.js"
OTHER_BUNDLE = "https://static.licdn.com/aero-v1/sc/h/unrelated-xyz.js"

REAL_IDS = {
    PROFILE_BY_VANITY: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    PROFILE_CARDS: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    PROFILE_COMPONENTS: "cccccccccccccccccccccccccccccccc",
}


def _bundle_js() -> str:
    """A JS bundle with the ids embedded the way LinkedIn ships them."""
    literals = ",".join(f'"{name}.{digest}"' for name, digest in REAL_IDS.items())
    return f"var e=[{literals}];function t(){{return e}}"


def _feed_html() -> str:
    return (
        "<html><head>"
        f'<link rel="preload" href="{OTHER_BUNDLE}" as="script">'
        f'<script src="{BUNDLE}"></script>'
        "</head><body></body></html>"
    )


# ----------------------------------------------------------------- discovery


@respx.mock
async def test_discovers_query_ids_from_the_javascript_bundles(client, tmp_path) -> None:
    """The recovery path: scrape the ids straight out of LinkedIn's own JS."""
    respx.get(FEED).mock(return_value=httpx.Response(200, text=_feed_html()))
    respx.get(BUNDLE).mock(return_value=httpx.Response(200, text=_bundle_js()))
    respx.get(OTHER_BUNDLE).mock(return_value=httpx.Response(200, text="var nothing=1;"))

    resolver = QueryIdResolver(path=tmp_path / "ids.json", discovery_enabled=True)
    assert resolver.is_usable is False, "starts with nothing known"

    await resolver.ensure_fresh(client)

    assert resolver.is_usable is True
    assert resolver.get(PROFILE_CARDS) == f"{PROFILE_CARDS}.{REAL_IDS[PROFILE_CARDS]}"
    assert resolver.source_of(PROFILE_CARDS) == "discovered"


@respx.mock
async def test_discovered_ids_are_persisted_so_a_restart_starts_warm(
    client, tmp_path
) -> None:
    respx.get(FEED).mock(return_value=httpx.Response(200, text=_feed_html()))
    respx.get(BUNDLE).mock(return_value=httpx.Response(200, text=_bundle_js()))
    respx.get(OTHER_BUNDLE).mock(return_value=httpx.Response(200, text=""))

    path = tmp_path / "ids.json"
    await QueryIdResolver(path=path, discovery_enabled=True).ensure_fresh(client)

    saved = json.loads(path.read_text())
    assert saved[PROFILE_CARDS] == f"{PROFILE_CARDS}.{REAL_IDS[PROFILE_CARDS]}"

    # A fresh resolver reads them back without touching the network.
    reloaded = QueryIdResolver(path=path, discovery_enabled=False)
    assert reloaded.is_usable is True
    assert reloaded.source_of(PROFILE_CARDS) == "override"


@respx.mock
async def test_discovery_failure_is_survivable(client, tmp_path) -> None:
    """A dead CDN must not raise — the other tiers still need to run."""
    respx.get(FEED).mock(return_value=httpx.Response(500))

    resolver = QueryIdResolver(path=tmp_path / "ids.json", discovery_enabled=True)
    await resolver.ensure_fresh(client)  # must not raise

    assert resolver.is_usable is False
    assert resolver.missing


@respx.mock
async def test_discovery_is_skipped_when_disabled(client, tmp_path) -> None:
    route = respx.get(FEED).mock(return_value=httpx.Response(200, text=_feed_html()))

    resolver = QueryIdResolver(path=tmp_path / "ids.json", discovery_enabled=False)
    await resolver.ensure_fresh(client)

    assert route.call_count == 0


# ------------------------------------------------------------------ overrides


def test_operator_override_is_loaded_from_disk(tmp_path) -> None:
    path = tmp_path / "ids.json"
    path.write_text(json.dumps({PROFILE_CARDS: f"{PROFILE_CARDS}.deadbeef"}))

    resolver = QueryIdResolver(path=path, discovery_enabled=False)

    assert resolver.get(PROFILE_CARDS) == f"{PROFILE_CARDS}.deadbeef"
    assert resolver.source_of(PROFILE_CARDS) == "override"


@respx.mock
async def test_operator_override_beats_discovery(client, tmp_path) -> None:
    """An operator correcting a hash by hand must not be overwritten."""
    path = tmp_path / "ids.json"
    path.write_text(json.dumps({PROFILE_CARDS: f"{PROFILE_CARDS}.operatorvalue"}))

    respx.get(FEED).mock(return_value=httpx.Response(200, text=_feed_html()))
    respx.get(BUNDLE).mock(return_value=httpx.Response(200, text=_bundle_js()))
    respx.get(OTHER_BUNDLE).mock(return_value=httpx.Response(200, text=""))

    resolver = QueryIdResolver(path=path, discovery_enabled=True)
    await resolver.ensure_fresh(client)

    assert resolver.get(PROFILE_CARDS) == f"{PROFILE_CARDS}.operatorvalue"
    # Ids the operator did not pin are still discovered.
    assert resolver.get(PROFILE_BY_VANITY).endswith(REAL_IDS[PROFILE_BY_VANITY])


def test_a_corrupt_override_file_does_not_crash_startup(tmp_path) -> None:
    path = tmp_path / "ids.json"
    path.write_text("{ not json at all")

    resolver = QueryIdResolver(path=path, discovery_enabled=False)
    assert resolver.is_usable is False


# --------------------------------------------------------------- re-discovery


@respx.mock
async def test_invalidate_forces_a_fresh_scan(client, tmp_path) -> None:
    """After LinkedIn rejects an id, the next call must re-scan, not reuse it."""
    respx.get(FEED).mock(return_value=httpx.Response(200, text=_feed_html()))
    bundle = respx.get(BUNDLE).mock(return_value=httpx.Response(200, text=_bundle_js()))
    respx.get(OTHER_BUNDLE).mock(return_value=httpx.Response(200, text=""))

    resolver = QueryIdResolver(path=tmp_path / "ids.json", discovery_enabled=True)
    await resolver.ensure_fresh(client)
    calls_after_first = bundle.call_count

    # Fresh: no rescan.
    await resolver.ensure_fresh(client)
    assert bundle.call_count == calls_after_first

    # Invalidated: rescan.
    resolver.invalidate(PROFILE_CARDS)
    await resolver.ensure_fresh(client)
    assert bundle.call_count > calls_after_first


@respx.mock
async def test_missing_ids_always_trigger_a_scan_regardless_of_freshness(
    client, tmp_path
) -> None:
    respx.get(FEED).mock(return_value=httpx.Response(200, text=_feed_html()))
    respx.get(BUNDLE).mock(return_value=httpx.Response(200, text=_bundle_js()))
    respx.get(OTHER_BUNDLE).mock(return_value=httpx.Response(200, text=""))

    resolver = QueryIdResolver(
        path=tmp_path / "ids.json", discovery_enabled=True, ttl_seconds=999_999
    )
    await resolver.ensure_fresh(client)
    assert resolver.is_usable


def test_snapshot_reports_status_for_readiness(tmp_path) -> None:
    resolver = QueryIdResolver(path=tmp_path / "ids.json", discovery_enabled=True)
    snap = resolver.snapshot()

    assert snap["usable"] is False
    assert set(snap["missing"]) == {PROFILE_BY_VANITY, PROFILE_CARDS, PROFILE_COMPONENTS}
    assert snap["discovery_enabled"] is True
