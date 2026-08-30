"""Session cookie persistence.

LinkedIn issues routing and tracking cookies (`lidc`, `bcookie`, `bscookie`)
and expects them echoed back. `lidc` in particular pins you to a datacenter;
without it LinkedIn 302s to the *same URL* trying to set it, which a client
that discards cookies experiences as an infinite redirect.

Observed live: following that redirect with a cookie jar got past it, while
rebuilding the header from only li_at + JSESSIONID looped forever. So the
session must accumulate what LinkedIn sets.
"""

from __future__ import annotations

import httpx
import respx

from app.linkedin.session import LinkedInSession

URL = "https://www.linkedin.com/voyager/api/me"


def test_session_accumulates_cookies_linkedin_sets() -> None:
    s = LinkedInSession(li_at="tok", jsessionid="ajax:1")
    s.absorb({"lidc": "b=VB1:x", "bcookie": "v=2&abc", "bscookie": "v=1&def"})

    header = s.cookie_header()
    assert "li_at=tok" in header
    assert 'JSESSIONID="ajax:1"' in header
    assert "lidc=b=VB1:x" in header
    assert "bcookie=v=2&abc" in header


def test_absorbing_never_overwrites_the_configured_li_at() -> None:
    """LinkedIn echoes li_at back; the operator's value stays authoritative."""
    s = LinkedInSession(li_at="operator-token", jsessionid="ajax:1")
    s.absorb({"li_at": "some-other-value", "lidc": "b=VB1:x"})

    assert "li_at=operator-token" in s.cookie_header()
    assert "some-other-value" not in s.cookie_header()


def test_jsessionid_from_set_cookie_updates_the_csrf_token() -> None:
    """A bootstrapped JSESSIONID must stay paired with the CSRF header."""
    s = LinkedInSession(li_at="tok", jsessionid="")
    s.absorb({"JSESSIONID": '"ajax:999"'})

    assert s.csrf_token == "ajax:999"
    assert 'JSESSIONID="ajax:999"' in s.cookie_header()


def test_deleted_cookies_are_dropped_not_stored() -> None:
    s = LinkedInSession(li_at="tok", jsessionid="ajax:1")
    s.absorb({"lidc": "b=VB1:x"})
    s.absorb({"lidc": ""})

    assert "lidc" not in s.cookie_header()


@respx.mock
async def test_client_echoes_cookies_back_on_the_next_request(client) -> None:
    """The end-to-end behaviour: request 2 must carry request 1's cookies."""
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"ok": 1},
                headers=[
                    ("set-cookie", "lidc=b=VB1:t=abc; Path=/; Domain=.linkedin.com"),
                    ("set-cookie", "bcookie=v=2&xyz; Path=/"),
                ],
            ),
            httpx.Response(200, json={"ok": 2}),
        ]
    )

    await client.get_json(URL)
    await client.get_json(URL)

    second = respx.calls[1].request.headers["cookie"]
    assert "lidc=b=VB1:t=abc" in second, "routing cookie must be echoed back"
    assert "bcookie=v=2&xyz" in second


@respx.mock
async def test_li_at_survives_after_linkedin_sets_cookies(client) -> None:
    """httpx's own jar must never clobber the session cookie header.

    httpx applies its cookie jar with `add_unredirected_header`, which
    *replaces* an existing Cookie header rather than merging. So once LinkedIn
    has set any cookie, a manually-built header silently loses `li_at` and every
    subsequent request goes out anonymous — which LinkedIn answers with the
    login page.
    """
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"n": 1},
                headers=[
                    ("set-cookie", 'JSESSIONID="ajax:aaa"; Path=/'),
                    ("set-cookie", "lidc=b=VB1:t=zzz; Path=/"),
                    ("set-cookie", "bcookie=v=2&q; Path=/"),
                ],
            ),
            httpx.Response(200, json={"n": 2}),
            httpx.Response(200, json={"n": 3}),
        ]
    )

    await client.get_json(URL)
    await client.get_json(URL)
    await client.get_json(URL)

    for i, call in enumerate(respx.calls, start=1):
        cookie = call.request.headers.get("cookie", "")
        assert "li_at=test-li-at" in cookie, f"request {i} lost li_at: {cookie!r}"
    # ...and the cookies LinkedIn issued are still carried alongside it.
    assert "lidc=b=VB1:t=zzz" in respx.calls[2].request.headers["cookie"]


@respx.mock
async def test_li_at_survives_a_redirect_chain(client) -> None:
    """The failure that actually bit us live.

    httpx applies its cookie jar when following a redirect, replacing the
    Cookie header built for the first hop. So hop 1 carries li_at, hop 2 goes
    out anonymous, and LinkedIn answers the second hop with the login page —
    which looks exactly like an invalid cookie.
    """
    respx.get("https://www.linkedin.com/step1").mock(
        return_value=httpx.Response(
            302,
            headers=[
                ("location", "https://www.linkedin.com/step2"),
                ("set-cookie", 'JSESSIONID="ajax:mid"; Path=/'),
                ("set-cookie", "lidc=b=VB1:t=mid; Path=/"),
            ],
        )
    )
    respx.get("https://www.linkedin.com/step2").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )

    await client.get_html("https://www.linkedin.com/step1")

    assert len(respx.calls) == 2, "the redirect should have been followed"
    for i, call in enumerate(respx.calls, start=1):
        cookie = call.request.headers.get("cookie", "")
        assert "li_at=test-li-at" in cookie, f"hop {i} went out without li_at: {cookie!r}"
