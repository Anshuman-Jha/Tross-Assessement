"""Transport behaviour: headers, status mapping, retries, session health.

These are the rules that decide whether a LinkedIn account survives contact
with this service, so they are asserted rather than assumed.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.linkedin.exceptions import (
    AuthenticationError,
    ChallengeError,
    ProfileNotFoundError,
    ProfilePrivateError,
    QueryIdError,
    RateLimitedError,
    UpstreamError,
)
from app.linkedin.session import SessionState

URL = "https://www.linkedin.com/voyager/api/me"


# ------------------------------------------------------------------- headers


@respx.mock
async def test_sends_the_headers_voyager_requires(client) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    await client.get_json(URL)

    sent = route.calls[0].request
    # The CSRF token is the JSESSIONID with its literal quotes stripped.
    assert sent.headers["csrf-token"] == "ajax:1234567890"
    # ...while the cookie keeps them.
    assert 'JSESSIONID="ajax:1234567890"' in sent.headers["cookie"]
    assert "li_at=test-li-at" in sent.headers["cookie"]
    # Rest.li 2.0 and the normalised response format.
    assert sent.headers["x-restli-protocol-version"] == "2.0.0"
    assert sent.headers["accept"] == "application/vnd.linkedin.normalized+json+2.1"
    # A missing Host header makes Voyager answer 400.
    assert sent.headers["host"] == "www.linkedin.com"
    assert "Chrome" in sent.headers["user-agent"]


@respx.mock
async def test_configuring_only_li_at_is_enough(client, session) -> None:
    """The CSRF token is negotiated, so operators need supply only li_at.

    See tests/integration/test_bootstrap.py for the bootstrap mechanics; this
    pins the operator-facing guarantee.
    """
    session.jsessionid = ""
    assert session.has_jsessionid is False

    respx.get("https://www.linkedin.com/feed/").mock(
        return_value=httpx.Response(
            200, text="<html></html>", headers={"set-cookie": 'JSESSIONID="ajax:99999"; Path=/'}
        )
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    assert await client.get_json(URL) == {"ok": True}
    assert session.csrf_token == "ajax:99999"


# ------------------------------------------------------------ status mapping


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, ProfileNotFoundError),
        (403, ProfilePrivateError),
        (401, AuthenticationError),
    ],
)
@respx.mock
async def test_status_codes_map_to_typed_errors(client, status, expected) -> None:
    respx.get(URL).mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(expected):
        await client.get_json(URL)


@respx.mock
async def test_a_403_mentioning_csrf_is_an_auth_error_not_a_private_profile(client) -> None:
    respx.get(URL).mock(return_value=httpx.Response(403, text="CSRF check failed"))
    with pytest.raises(AuthenticationError):
        await client.get_json(URL)


@respx.mock
async def test_400_naming_queryid_becomes_a_queryid_error(client) -> None:
    """This is the signal that LinkedIn rotated its hashes."""
    respx.get(URL).mock(
        return_value=httpx.Response(400, text='{"message":"unknown queryId"}')
    )
    with pytest.raises(QueryIdError):
        await client.get_json(URL)


@respx.mock
async def test_html_returned_on_a_200_is_treated_as_a_login_wall(client) -> None:
    """LinkedIn serves the authwall with a 200; naive clients parse it as data."""
    respx.get(URL).mock(
        return_value=httpx.Response(200, text="<html><body>authwall</body></html>")
    )
    with pytest.raises(AuthenticationError):
        await client.get_json(URL)


@respx.mock
async def test_redirect_to_checkpoint_is_a_challenge(client) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://www.linkedin.com/checkpoint/challenge"})
    )
    with pytest.raises(ChallengeError):
        await client.get_json(URL)


@respx.mock
async def test_linkedin_999_is_a_rate_limit(client) -> None:
    """999 is LinkedIn's nonstandard block status, not a normal HTTP code."""
    respx.get(URL).mock(return_value=httpx.Response(999, text="blocked"))
    with pytest.raises(RateLimitedError):
        await client.get_json(URL)


# ------------------------------------------------------ retries and health


@respx.mock
async def test_transient_5xx_is_retried_then_succeeds(client) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(503, text="try later"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert await client.get_json(URL) == {"ok": True}


@respx.mock
async def test_auth_failures_are_never_retried(client, session) -> None:
    """Retrying a 401 cannot help and only accelerates a restriction."""
    route = respx.get(URL).mock(return_value=httpx.Response(401, json={}))

    with pytest.raises(AuthenticationError):
        await client.get_json(URL)

    assert route.call_count == 1, "a 401 must not be retried"
    assert session.state is SessionState.DEAD, "the session must be taken out of service"


@respx.mock
async def test_rate_limiting_cools_the_session_down(client, session) -> None:
    respx.get(URL).mock(return_value=httpx.Response(429, headers={"retry-after": "1"}))

    with pytest.raises(RateLimitedError):
        await client.get_json(URL)

    assert session.state is SessionState.COOLING_DOWN


@respx.mock
async def test_timeouts_surface_as_upstream_errors(client) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(UpstreamError):
        await client.get_json(URL)
