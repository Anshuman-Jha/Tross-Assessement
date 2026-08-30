"""Recorded samples: a demo that works without a live session, honestly.

A deployed demo whose LinkedIn cookie has expired can otherwise only show an
error, which tells a visitor nothing. The repository therefore ships a real
captured response replayed through the ordinary parsers.

What these tests defend is the honesty of that, not just the convenience:
a recording must never be presentable as a live fetch, and must never mask a
real answer from LinkedIn.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.linkedin.exceptions import (
    AuthenticationError,
    NoHealthySessionError,
    ProfileNotFoundError,
    ProfilePrivateError,
)
from app.main import create_app
from app.services.cache import InMemoryCache
from app.services.profile_service import ProfileService
from app.services.sample_store import available_samples, build_sample_response, has_sample


class FailingFetcher:
    """Stands in for a service whose credential does not work."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    async def fetch(self, public_id: str):  # noqa: ANN201
        self.calls += 1
        raise self._error


@pytest.fixture
def make_client():
    clients: list[TestClient] = []

    def _build(fetcher, **kwargs) -> TestClient:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        app.state.profile_service = ProfileService(
            fetcher, InMemoryCache(max_entries=8), ttl=60, **kwargs
        )
        clients.append(client)
        return client

    yield _build
    for c in clients:
        c.__exit__(None, None, None)


# ------------------------------------------------------------------ the store


def test_a_sample_is_shipped_and_parses() -> None:
    assert "ada-lovelace" in available_samples()
    assert has_sample("ada-lovelace") and has_sample("ADA-LOVELACE")
    assert not has_sample("someone-else")

    sample = build_sample_response("ada-lovelace")
    assert sample is not None
    # It is real captured data, so it carries real structure.
    assert sample.profile.basics.full_name == "Ada Lovelace"
    assert sample.profile.experience, "the recording should include experience"
    assert sample.profile.education


def test_a_sample_is_labelled_as_a_recording() -> None:
    """The whole point: it must be impossible to mistake for a live fetch."""
    sample = build_sample_response("ada-lovelace")
    assert sample is not None

    assert sample.meta.source.value == "recorded_sample"
    assert sample.meta.is_live is False
    assert sample.warnings, "a recording must announce itself"
    assert "RECORDED" in sample.warnings[0]


# ------------------------------------------------------------- when it applies


@pytest.mark.parametrize(
    "error",
    [
        NoHealthySessionError("no sessions configured"),
        AuthenticationError("cookie expired"),
    ],
)
def test_credential_failure_falls_back_to_the_recording(make_client, auth_headers, error) -> None:
    client = make_client(FailingFetcher(error))
    response = client.get(
        "/api/v1/profile", params={"url": "ada-lovelace"}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["source"] == "recorded_sample"
    assert body["meta"]["is_live"] is False
    assert body["profile"]["basics"]["full_name"] == "Ada Lovelace"
    assert any("RECORDED" in w for w in body["warnings"])


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ProfileNotFoundError("no such profile"), 404),
        (ProfilePrivateError("private"), 403),
    ],
)
def test_a_real_answer_from_linkedin_is_never_masked(
    make_client, auth_headers, error, status
) -> None:
    """A 404 is a genuine answer. Substituting a recording would be a lie."""
    client = make_client(FailingFetcher(error))
    response = client.get(
        "/api/v1/profile", params={"url": "ada-lovelace"}, headers=auth_headers
    )

    assert response.status_code == status
    assert response.json()["success"] is False


def test_profiles_without_a_recording_still_error(make_client, auth_headers) -> None:
    client = make_client(FailingFetcher(NoHealthySessionError("none")))
    response = client.get(
        "/api/v1/profile", params={"url": "williamhgates"}, headers=auth_headers
    )

    assert response.status_code == 503
    assert response.json()["error"] == "NO_HEALTHY_SESSION"


def test_samples_can_be_disabled(make_client, auth_headers) -> None:
    client = make_client(FailingFetcher(NoHealthySessionError("none")), samples_enabled=False)
    response = client.get(
        "/api/v1/profile", params={"url": "ada-lovelace"}, headers=auth_headers
    )

    assert response.status_code == 503


def test_a_live_session_always_wins(make_client, auth_headers) -> None:
    """The recording is a fallback, never a shortcut past a working fetch."""
    from app.linkedin.fetcher import FetchResult
    from app.models.profile import Basics, Profile, Source

    class LiveFetcher:
        async def fetch(self, public_id: str) -> FetchResult:
            r = FetchResult(
                profile=Profile(basics=Basics(full_name="Live Person")),
                source=Source.DASH,
            )
            r.mark_completeness()
            return r

    client = make_client(LiveFetcher())
    body = client.get(
        "/api/v1/profile", params={"url": "ada-lovelace"}, headers=auth_headers
    ).json()

    assert body["meta"]["source"] == "voyager_dash"
    assert body["meta"]["is_live"] is True
    assert body["profile"]["basics"]["full_name"] == "Live Person"
