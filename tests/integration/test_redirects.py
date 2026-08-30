"""Redirect handling and expired-cookie detection.

Both behaviours were found by probing live LinkedIn:

* LinkedIn 301-redirects ``/in/<slug>/`` to ``/in/<slug>``. Treating that as an
  error breaks the HTML tier even when the session is perfectly valid.
* When a session cookie is stale, LinkedIn answers 302 and *deletes* ``li_at``
  via ``Set-Cookie`` with a 1970 expiry. That is the single most useful signal
  for telling an operator their cookie needs replacing, so it must not be
  reported as a generic "unexpected redirect".
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.linkedin.endpoints import profile_html_url
from app.linkedin.exceptions import AuthenticationError, UpstreamError

PROFILE = "https://www.linkedin.com/in/testuser"


def test_profile_html_url_has_no_trailing_slash() -> None:
    """LinkedIn 301s the trailing-slash form, costing a needless round trip."""
    assert profile_html_url("testuser") == PROFILE


@respx.mock
async def test_same_host_redirect_is_followed(client) -> None:
    respx.get(PROFILE + "/").mock(
        return_value=httpx.Response(301, headers={"location": PROFILE})
    )
    respx.get(PROFILE).mock(return_value=httpx.Response(200, text="<html>profile</html>"))

    assert "profile" in await client.get_html(PROFILE + "/")


@respx.mock
async def test_relative_redirect_is_resolved(client) -> None:
    respx.get(PROFILE + "/").mock(
        return_value=httpx.Response(301, headers={"location": "/in/testuser"})
    )
    respx.get(PROFILE).mock(return_value=httpx.Response(200, text="<html>ok</html>"))

    assert "ok" in await client.get_html(PROFILE + "/")


@respx.mock
async def test_deleted_li_at_cookie_is_reported_as_an_expired_session(client) -> None:
    """The exact signature live LinkedIn returns for a stale cookie."""
    respx.get(PROFILE).mock(
        return_value=httpx.Response(
            302,
            headers=[
                ("location", PROFILE),
                ("set-cookie", "li_at=delete; Expires=Thu, 01-Jan-1970 00:00:00 GMT; Max-Age=0"),
                ("set-cookie", "liap=delete; Expires=Thu, 01-Jan-1970 00:00:00 GMT"),
            ],
        )
    )

    with pytest.raises(AuthenticationError, match=r"expired|invalid|revoked"):
        await client.get_html(PROFILE)


@respx.mock
async def test_a_redirect_loop_does_not_spin_forever(client) -> None:
    respx.get(PROFILE).mock(return_value=httpx.Response(302, headers={"location": PROFILE}))

    with pytest.raises((UpstreamError, AuthenticationError)):
        await client.get_html(PROFILE)


@respx.mock
async def test_offsite_redirects_are_refused(client) -> None:
    """Never follow a redirect off linkedin.com while carrying session cookies."""
    respx.get(PROFILE).mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example.com/steal"})
    )

    with pytest.raises(UpstreamError):
        await client.get_html(PROFILE)


@respx.mock
async def test_authwall_redirect_still_maps_to_auth_error(client) -> None:
    respx.get(PROFILE).mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.linkedin.com/authwall?trk=x"}
        )
    )

    with pytest.raises(AuthenticationError):
        await client.get_html(PROFILE)
