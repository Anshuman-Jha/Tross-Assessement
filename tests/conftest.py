"""Shared fixtures.

Every test in this suite runs fully offline. LinkedIn is mocked at the HTTP
transport layer with ``respx``, so CI never needs a session cookie and never
sends traffic to LinkedIn.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Configure the app before anything imports the settings singleton.
os.environ.setdefault("LINKEDIN_LI_AT", "test-li-at-cookie")
os.environ.setdefault("LINKEDIN_JSESSIONID", "ajax:1234567890")
os.environ.setdefault("API_KEYS", "test-key")
os.environ.setdefault("REQUIRE_API_KEY", "true")
os.environ.setdefault("ENABLE_QUERY_ID_DISCOVERY", "false")
os.environ.setdefault("MIN_DELAY_BETWEEN_REQUESTS_MS", "0")
os.environ.setdefault("JITTER_MS", "0")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("QUERY_ID_FILE", "")


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Settings are cached with lru_cache; clear between tests."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def api_key() -> str:
    return "test-key"


@pytest.fixture
def auth_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


@pytest.fixture
def session():
    from app.linkedin.session import LinkedInSession

    return LinkedInSession(li_at="test-li-at", jsessionid="ajax:1234567890", label="test")


@pytest.fixture
def pool(session):
    from app.linkedin.session_pool import SessionPool

    return SessionPool([session], requests_per_minute=1000, cooldown_seconds=1)


@pytest.fixture
async def client(pool):
    """A VoyagerClient with pacing disabled so tests do not sleep."""
    from app.linkedin.client import VoyagerClient

    voyager = VoyagerClient(pool, min_delay_ms=0, jitter_ms=0, max_retries=1, timeout=5.0)
    try:
        yield voyager
    finally:
        await voyager.aclose()


@pytest.fixture
def resolver(tmp_path):
    """A resolver with known ids and discovery off, for deterministic tests."""
    from app.linkedin.query_ids import (
        PROFILE_BY_VANITY,
        PROFILE_CARDS,
        PROFILE_COMPONENTS,
        QueryIdResolver,
    )

    r = QueryIdResolver(path=tmp_path / "query_ids.json", discovery_enabled=False)
    r._ids = {
        PROFILE_BY_VANITY: f"{PROFILE_BY_VANITY}.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        PROFILE_CARDS: f"{PROFILE_CARDS}.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        PROFILE_COMPONENTS: f"{PROFILE_COMPONENTS}.cccccccccccccccccccccccccccccccc",
    }
    r._source = dict.fromkeys(r._ids, "test")
    return r
