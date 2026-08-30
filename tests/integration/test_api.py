"""HTTP surface: auth, validation, error envelopes, caching, ops endpoints.

The acquisition stack is stubbed here on purpose — tiers and parsing are
covered elsewhere. What is under test is the contract the API presents.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.linkedin.exceptions import (
    NoHealthySessionError,
    ProfileNotFoundError,
    ProfilePrivateError,
    RateLimitedError,
)
from app.linkedin.fetcher import FetchResult
from app.main import create_app
from app.models.profile import Basics, Experience, Profile, Source
from app.services.cache import InMemoryCache
from app.services.profile_service import ProfileService


class StubFetcher:
    """Stands in for the acquisition stack; counts calls to prove caching."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    async def fetch(self, public_id: str) -> FetchResult:
        self.calls += 1
        if self._error:
            raise self._error
        result = self._result or _sample_result()
        result.mark_completeness()
        return result


def _sample_result() -> FetchResult:
    return FetchResult(
        profile=Profile(
            basics=Basics(
                first_name="Ada",
                last_name="Lovelace",
                full_name="Ada Lovelace",
                headline="Mathematician",
            ),
            experience=[Experience(title="Analyst", company="Analytical Engine Co")],
        ),
        source=Source.GRAPHQL,
        profile_urn="urn:li:fsd_profile:ACoAAATEST",
    )


@pytest.fixture
def make_client():
    """Build a TestClient whose service is backed by a stub fetcher."""
    clients: list[TestClient] = []

    def _build(fetcher: StubFetcher) -> TestClient:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()  # run lifespan so app.state is populated
        app.state.profile_service = ProfileService(
            fetcher, InMemoryCache(max_entries=16), ttl=60
        )
        clients.append(client)
        return client

    yield _build
    for c in clients:
        c.__exit__(None, None, None)


# ------------------------------------------------------------------ happy path


def test_get_profile_returns_the_documented_envelope(make_client, auth_headers) -> None:
    client = make_client(StubFetcher())
    response = client.get(
        "/api/v1/profile", params={"url": "https://www.linkedin.com/in/testuser/"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["profile"]["basics"]["full_name"] == "Ada Lovelace"
    assert body["profile"]["experience"][0]["title"] == "Analyst"

    meta = body["meta"]
    assert meta["public_identifier"] == "testuser"
    assert meta["profile_url"] == "https://www.linkedin.com/in/testuser"
    assert meta["source"] == "voyager_graphql"
    assert meta["cached"] is False
    assert meta["completeness"]["experience"] is True
    # Sections the profile lacks are present and empty, never missing.
    assert body["profile"]["certifications"] == []
    assert body["profile"]["languages"] == []


def test_post_and_get_and_slug_forms_agree(make_client, auth_headers) -> None:
    client = make_client(StubFetcher())
    a = client.post(
        "/api/v1/profile", json={"url": "https://www.linkedin.com/in/testuser/"},
        headers=auth_headers,
    ).json()
    b = client.get(
        "/api/v1/profile", params={"url": "testuser"}, headers=auth_headers
    ).json()
    c = client.get("/api/v1/profile/testuser", headers=auth_headers).json()

    ids = {r["meta"]["public_identifier"] for r in (a, b, c)}
    assert ids == {"testuser"}


# ---------------------------------------------------------------------- cache


def test_repeat_requests_hit_the_cache_not_linkedin(make_client, auth_headers) -> None:
    """Every cache miss is upstream load on a rate-limited account."""
    fetcher = StubFetcher()
    client = make_client(fetcher)
    params = {"url": "https://www.linkedin.com/in/testuser/"}

    first = client.get("/api/v1/profile", params=params, headers=auth_headers).json()
    second = client.get("/api/v1/profile", params=params, headers=auth_headers).json()

    assert fetcher.calls == 1, "the second request must not reach LinkedIn"
    assert first["meta"]["cached"] is False
    assert second["meta"]["cached"] is True
    assert second["profile"] == first["profile"]


def test_refresh_bypasses_the_cache(make_client, auth_headers) -> None:
    fetcher = StubFetcher()
    client = make_client(fetcher)
    params = {"url": "testuser"}

    client.get("/api/v1/profile", params=params, headers=auth_headers)
    body = client.get(
        "/api/v1/profile", params={**params, "refresh": "true"}, headers=auth_headers
    ).json()

    assert fetcher.calls == 2
    assert body["meta"]["cached"] is False


def test_different_url_shapes_share_one_cache_entry(make_client, auth_headers) -> None:
    fetcher = StubFetcher()
    client = make_client(fetcher)
    for url in (
        "https://www.linkedin.com/in/testuser/",
        "https://uk.linkedin.com/in/testuser?trk=x",
        "testuser",
    ):
        client.get("/api/v1/profile", params={"url": url}, headers=auth_headers)

    assert fetcher.calls == 1, "normalisation must collapse these to one key"


# ----------------------------------------------------------------------- auth


def test_missing_api_key_is_rejected(make_client) -> None:
    client = make_client(StubFetcher())
    response = client.get("/api/v1/profile", params={"url": "testuser"})

    assert response.status_code == 401
    assert response.json()["error"] == "UNAUTHORIZED"


def test_wrong_api_key_is_rejected(make_client) -> None:
    client = make_client(StubFetcher())
    response = client.get(
        "/api/v1/profile", params={"url": "testuser"}, headers={"X-API-Key": "nope"}
    )
    assert response.status_code == 401


# ----------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "url",
    ["https://example.com/in/x", "https://www.linkedin.com/company/acme", "not a url"],
)
def test_non_profile_urls_are_422(make_client, auth_headers, url) -> None:
    client = make_client(StubFetcher())
    response = client.get("/api/v1/profile", params={"url": url}, headers=auth_headers)

    assert response.status_code == 422
    assert response.json()["error"] == "INVALID_PROFILE_URL"


def test_missing_url_parameter_is_422(make_client, auth_headers) -> None:
    client = make_client(StubFetcher())
    response = client.get("/api/v1/profile", headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["error"] == "VALIDATION_ERROR"


# -------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ProfileNotFoundError("gone"), 404, "PROFILE_NOT_FOUND"),
        (ProfilePrivateError("private"), 403, "PROFILE_PRIVATE"),
        (NoHealthySessionError("no sessions"), 503, "NO_HEALTHY_SESSION"),
        (RateLimitedError("slow down", retry_after=30), 429, "RATE_LIMITED"),
    ],
)
def test_upstream_errors_map_to_typed_responses(
    make_client, auth_headers, error, status, code
) -> None:
    client = make_client(StubFetcher(error=error))
    response = client.get(
        "/api/v1/profile", params={"url": "testuser"}, headers=auth_headers
    )

    assert response.status_code == status
    body = response.json()
    assert body["success"] is False
    assert body["error"] == code
    assert body["message"]
    assert body["request_id"], "every error must be traceable to a request id"


def test_rate_limit_sets_retry_after(make_client, auth_headers) -> None:
    client = make_client(StubFetcher(error=RateLimitedError("slow", retry_after=30)))
    response = client.get(
        "/api/v1/profile", params={"url": "testuser"}, headers=auth_headers
    )
    assert response.headers["Retry-After"] == "30"


def test_unexpected_errors_do_not_leak_internals(make_client, auth_headers) -> None:
    client = make_client(StubFetcher(error=RuntimeError("secret path /etc/passwd")))
    response = client.get(
        "/api/v1/profile", params={"url": "testuser"}, headers=auth_headers
    )

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "INTERNAL_ERROR"
    assert "secret path" not in response.text, "internals must not reach the client"


# ------------------------------------------------------------------------ ops


def test_healthz_is_always_ok(make_client) -> None:
    client = make_client(StubFetcher())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_reports_session_health_without_leaking_cookies(make_client) -> None:
    client = make_client(StubFetcher())
    response = client.get("/readyz")

    assert response.status_code in (200, 503)
    text = response.text
    assert "test-li-at" not in text, "readiness must never expose cookie values"
    assert "ajax:1234567890" not in text
    assert "sessions" in response.json()


def test_openapi_documents_the_endpoints(make_client) -> None:
    client = make_client(StubFetcher())
    spec = client.get("/openapi.json").json()

    assert "/api/v1/profile" in spec["paths"]
    assert "get" in spec["paths"]["/api/v1/profile"]
    assert "post" in spec["paths"]["/api/v1/profile"]
    assert "ProfileResponse" in spec["components"]["schemas"]


def test_request_id_is_echoed_for_tracing(make_client, auth_headers) -> None:
    client = make_client(StubFetcher())
    response = client.get(
        "/api/v1/profile",
        params={"url": "testuser"},
        headers={**auth_headers, "X-Request-ID": "trace-me-123"},
    )
    assert response.headers["X-Request-ID"] == "trace-me-123"


# ------------------------------------------------- PhantomBuster flat format


def test_flat_format_returns_phantombuster_style_columns(make_client, auth_headers) -> None:
    """The PDF cites PhantomBuster, whose output is a flat CSV-shaped record."""
    client = make_client(StubFetcher())
    body = client.get(
        "/api/v1/profile",
        params={"url": "testuser", "format": "flat"},
        headers=auth_headers,
    ).json()

    # Single level: no nested objects, which is what makes it CSV-ready.
    assert all(not isinstance(v, (dict, list)) for v in body.values()), body

    assert body["fullName"] == "Ada Lovelace"
    assert body["headline"] == "Mathematician"
    assert body["jobTitle"] == "Analyst"
    assert body["company"] == "Analytical Engine Co"
    assert body["profileUrl"] == "https://www.linkedin.com/in/testuser"
    assert body["experienceCount"] == 1
    assert body["timestamp"]


def test_nested_is_the_default_format(make_client, auth_headers) -> None:
    client = make_client(StubFetcher())
    body = client.get(
        "/api/v1/profile", params={"url": "testuser"}, headers=auth_headers
    ).json()
    assert "profile" in body and "meta" in body


def test_an_unknown_format_is_rejected(make_client, auth_headers) -> None:
    client = make_client(StubFetcher())
    response = client.get(
        "/api/v1/profile",
        params={"url": "testuser", "format": "xml"},
        headers=auth_headers,
    )
    assert response.status_code == 422


# ------------------------------------------------------------------- console UI


def test_root_serves_the_console_ui(make_client) -> None:
    client = make_client(StubFetcher())
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<title>" in body
    # The UI's job is to make the acquisition path visible.
    assert "voyager_graphql" in body
    assert "Acquisition trace" in body


def test_service_descriptor_still_available_as_json(make_client) -> None:
    client = make_client(StubFetcher())
    body = client.get("/api").json()

    assert body["service"] == "LinkedIn Profile API"
    assert body["ui"] == "/"
    assert "GET /api/v1/profile?url=..." in body["endpoints"]


def test_ui_does_not_embed_any_credential(make_client) -> None:
    """The page is served to anyone; it must never carry server-side secrets."""
    client = make_client(StubFetcher())
    body = client.get("/").text

    assert "test-li-at" not in body
    assert "ajax:1234567890" not in body
    assert "test-key" not in body, "API keys are entered by the user, never baked in"
