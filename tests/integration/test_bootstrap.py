"""Session bootstrap.

Voyager rejects an API call that carries no CSRF token, and the CSRF token *is*
the JSESSIONID cookie. A browser never hits this because it has always loaded a
page first and been issued one.

So when a session has no JSESSIONID, the client performs one navigation request
to obtain it (plus LinkedIn's routing cookies) before issuing API calls. This
also guarantees the CSRF pair is matched by construction, which is what makes
supplying JSESSIONID optional.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.linkedin.exceptions import AuthenticationError

FEED = "https://www.linkedin.com/feed/"
API = "https://www.linkedin.com/voyager/api/me"


def _bootstrap_response() -> httpx.Response:
    return httpx.Response(
        200,
        text="<html>feed</html>",
        headers=[
            ("set-cookie", 'JSESSIONID="ajax:9876543210"; Path=/; Domain=.linkedin.com'),
            ("set-cookie", "lidc=b=VB1:t=xyz; Path=/; Domain=.linkedin.com"),
            ("set-cookie", "bcookie=v=2&abc; Path=/; Domain=.linkedin.com"),
        ],
    )


@respx.mock
async def test_api_call_bootstraps_a_session_when_csrf_is_missing(client, session) -> None:
    session.jsessionid = ""
    feed = respx.get(FEED).mock(return_value=_bootstrap_response())
    api = respx.get(API).mock(return_value=httpx.Response(200, json={"ok": True}))

    assert await client.get_json(API) == {"ok": True}

    assert feed.call_count == 1, "must load a page to obtain a JSESSIONID first"
    assert session.csrf_token == "ajax:9876543210"

    sent = api.calls[0].request.headers
    assert sent["csrf-token"] == "ajax:9876543210", "CSRF pair must be matched"
    assert 'JSESSIONID="ajax:9876543210"' in sent["cookie"]
    assert "lidc=b=VB1:t=xyz" in sent["cookie"], "routing cookie must be carried"


@respx.mock
async def test_bootstrap_runs_only_once(client, session) -> None:
    session.jsessionid = ""
    feed = respx.get(FEED).mock(return_value=_bootstrap_response())
    respx.get(API).mock(return_value=httpx.Response(200, json={"ok": True}))

    await client.get_json(API)
    await client.get_json(API)

    assert feed.call_count == 1, "a session bootstraps once, not per request"


@respx.mock
async def test_no_bootstrap_when_jsessionid_already_supplied(client, session) -> None:
    """An operator-supplied pair is used as-is; no extra request."""
    feed = respx.get(FEED).mock(return_value=_bootstrap_response())
    respx.get(API).mock(return_value=httpx.Response(200, json={"ok": True}))

    await client.get_json(API)

    assert feed.call_count == 0


@respx.mock
async def test_login_redirect_during_bootstrap_is_an_auth_error(client, session) -> None:
    """A page load bouncing to /uas/login means li_at is not authenticating."""
    session.jsessionid = ""
    respx.get(FEED).mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.linkedin.com/uas/login"}
        )
    )
    respx.get("https://www.linkedin.com/uas/login").mock(
        return_value=httpx.Response(200, text="<html>Sign in</html>")
    )

    with pytest.raises(AuthenticationError, match=r"login|not valid|invalid"):
        await client.get_json(API)



@respx.mock
async def test_bootstrap_follows_redirects_and_collects_cookies_along_the_way(
    client, session
) -> None:
    """A page navigation follows redirects; cookies may be set on any hop.

    Live LinkedIn answers the first /feed/ request with a redirect and issues
    JSESSIONID further along the chain, so a non-following bootstrap sees no
    cookie and wrongly concludes the credential is bad.
    """
    session.jsessionid = ""
    respx.get(FEED).mock(
        return_value=httpx.Response(
            302,
            headers=[
                ("location", "https://www.linkedin.com/feed/home"),
                ("set-cookie", "lidc=b=VB1:t=hop1; Path=/"),
            ],
        )
    )
    respx.get("https://www.linkedin.com/feed/home").mock(
        return_value=httpx.Response(
            200,
            text="<html>feed</html>",
            headers=[("set-cookie", 'JSESSIONID="ajax:from-hop-2"; Path=/')],
        )
    )
    api = respx.get(API).mock(return_value=httpx.Response(200, json={"ok": True}))

    await client.get_json(API)

    assert session.csrf_token == "ajax:from-hop-2", "cookie from a later hop must be kept"
    cookie = api.calls[0].request.headers["cookie"]
    assert "lidc=b=VB1:t=hop1" in cookie, "cookie from the first hop must also be kept"
