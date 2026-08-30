"""Cache, session pool, HTML extraction, and log redaction."""

from __future__ import annotations

import asyncio

import pytest

from app.linkedin.exceptions import NoHealthySessionError
from app.linkedin.html_fallback import (
    extract_embedded_payloads,
    has_profile_content,
    looks_like_authwall,
    merge_payloads,
    payload_from_html,
)
from app.linkedin.session import LinkedInSession, SessionState
from app.linkedin.session_pool import SessionPool
from app.observability.logging import _scrub
from app.services.cache import InMemoryCache, build_cache

# ------------------------------------------------------------------- cache


async def test_cache_round_trip_and_miss() -> None:
    cache = InMemoryCache()
    assert await cache.get("nope") is None

    await cache.set("k", {"v": 1}, ttl=60)
    assert await cache.get("k") == {"v": 1}

    await cache.delete("k")
    assert await cache.get("k") is None


async def test_cache_expires_entries() -> None:
    cache = InMemoryCache()
    await cache.set("k", {"v": 1}, ttl=0)
    await asyncio.sleep(0.01)
    assert await cache.get("k") is None


async def test_cache_evicts_least_recently_used() -> None:
    cache = InMemoryCache(max_entries=2)
    await cache.set("a", {"n": 1}, ttl=60)
    await cache.set("b", {"n": 2}, ttl=60)
    await cache.get("a")  # 'a' becomes most recently used
    await cache.set("c", {"n": 3}, ttl=60)

    assert await cache.get("b") is None, "the least recently used entry should go"
    assert await cache.get("a") is not None
    assert await cache.get("c") is not None


def test_unreachable_redis_falls_back_to_memory_rather_than_failing_boot() -> None:
    cache = build_cache("redis://nonexistent-host-xyz:6379/0")
    assert cache.backend in ("redis", "memory")
    assert build_cache(None).backend == "memory"


# ------------------------------------------------------------- session pool


async def test_pool_prefers_the_least_recently_used_session() -> None:
    a = LinkedInSession(li_at="a", label="a")
    b = LinkedInSession(li_at="b", label="b")
    pool = SessionPool([a, b], requests_per_minute=1000)

    first = await pool.acquire()
    second = await pool.acquire()
    assert {first.label, second.label} == {"a", "b"}, "load must spread across sessions"


async def test_dead_sessions_are_removed_from_service() -> None:
    session = LinkedInSession(li_at="a", label="a")
    pool = SessionPool([session])
    pool.report_error(session, "401", fatal=True)

    assert session.state is SessionState.DEAD
    with pytest.raises(NoHealthySessionError, match="cookie expired or revoked"):
        await pool.acquire()


async def test_cooldown_expires_and_the_session_returns() -> None:
    session = LinkedInSession(li_at="a", label="a")
    pool = SessionPool([session])
    pool.report_error(session, "429", cooldown=0.05)

    with pytest.raises(NoHealthySessionError):
        await pool.acquire()

    await asyncio.sleep(0.06)
    assert (await pool.acquire()).label == "a"


async def test_repeated_errors_eventually_cool_a_session_down() -> None:
    session = LinkedInSession(li_at="a", label="a")
    pool = SessionPool([session], max_consecutive_errors=3, cooldown_seconds=60)

    for _ in range(3):
        pool.report_error(session, "flaky")

    assert session.state is SessionState.COOLING_DOWN


async def test_an_empty_pool_explains_itself() -> None:
    with pytest.raises(NoHealthySessionError, match="LINKEDIN_LI_AT"):
        await SessionPool([]).acquire()


def test_session_snapshot_never_contains_the_cookie() -> None:
    session = LinkedInSession(li_at="SECRET_COOKIE", jsessionid="ajax:999", label="a")
    snapshot = repr(session.snapshot())

    assert "SECRET_COOKIE" not in snapshot
    assert "ajax:999" not in snapshot
    assert snapshot.count("a") > 0


# ----------------------------------------------------------- html extraction


def test_extracts_and_merges_embedded_payloads() -> None:
    page = (
        "<html><body>"
        '<code style="display:none" id="bpr-guid-1">'
        '{"data":{"x":1},"included":[{"entityUrn":"urn:li:a","$type":"Profile"}]}'
        "</code>"
        '<code style="display:none" id="bpr-guid-2">'
        '{"data":{},"included":[{"entityUrn":"urn:li:b","$type":"Position"}]}'
        "</code>"
        "</body></html>"
    )
    payloads = extract_embedded_payloads(page)
    assert len(payloads) == 2

    merged = merge_payloads(payloads)
    assert {e["entityUrn"] for e in merged["included"]} == {"urn:li:a", "urn:li:b"}


def test_html_escaped_payloads_are_decoded() -> None:
    """LinkedIn HTML-escapes every quote inside these blocks."""
    page = (
        '<code id="bpr-guid-1">'
        "{&quot;data&quot;:{},&quot;included&quot;:"
        "[{&quot;entityUrn&quot;:&quot;urn:li:a&quot;,&quot;$type&quot;:&quot;Profile&quot;}]}"
        "</code>"
    )
    payloads = extract_embedded_payloads(page)
    assert len(payloads) == 1
    assert payloads[0]["included"][0]["entityUrn"] == "urn:li:a"


def test_duplicate_entities_are_deduplicated_when_merging() -> None:
    dup = {"entityUrn": "urn:li:a", "$type": "Profile"}
    merged = merge_payloads([{"included": [dup]}, {"included": [dup]}])
    assert len(merged["included"]) == 1


def test_non_json_blocks_are_ignored_without_raising() -> None:
    page = '<code id="bpr-guid-1">not json at all</code><code id="bpr-guid-2">{{{</code>'
    assert extract_embedded_payloads(page) == []


def test_authwall_is_detected() -> None:
    assert looks_like_authwall("<html><body>Join LinkedIn to see this</body></html>")
    assert looks_like_authwall('<a href="/uas/login">Sign in</a>')
    assert not looks_like_authwall("<html><body>A normal profile page</body></html>")


def test_profile_content_detection() -> None:
    assert has_profile_content(
        {"included": [{"$type": "com.linkedin.voyager.identity.profile.Profile"}]}
    )
    assert has_profile_content({"included": [{"topComponents": []}]})
    assert not has_profile_content({"included": []})
    assert not has_profile_content({"included": [{"$type": "something.Unrelated"}]})
    assert not has_profile_content({})


def test_a_page_with_no_payloads_yields_an_empty_envelope() -> None:
    result = payload_from_html("<html><body>nothing here</body></html>")
    assert result == {"data": {}, "included": []}


# ------------------------------------------------------------- log redaction


def test_credentials_are_redacted_from_logs() -> None:
    """A cookie leaked into a log aggregator is a compromised account."""
    scrubbed = _scrub(
        {
            "li_at": "SECRET",
            "jsessionid": "ajax:123",
            "cookie": "li_at=SECRET; JSESSIONID=ajax:123",
            "api_key": "my-key",
            "nested": {"password": "hunter2", "safe": "visible"},
            "message": "sent with li_at=SECRET and more",
            "public_id": "williamhgates",
        }
    )

    flat = repr(scrubbed)
    assert "SECRET" not in flat
    assert "hunter2" not in flat
    assert "my-key" not in flat
    # Non-sensitive fields survive, or the logs become useless.
    assert scrubbed["public_id"] == "williamhgates"
    assert scrubbed["nested"]["safe"] == "visible"


def test_redaction_handles_lists_and_deep_nesting() -> None:
    scrubbed = _scrub({"sessions": [{"li_at": "cookie_one"}, {"li_at": "cookie_two"}]})
    assert "cookie_one" not in repr(scrubbed)
    assert "cookie_two" not in repr(scrubbed)
    assert repr(scrubbed).count("REDACTED") == 2
