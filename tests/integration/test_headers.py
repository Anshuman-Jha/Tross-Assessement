"""Request fingerprinting: API calls and page loads must look different.

LinkedIn distinguishes an XHR from a page navigation, and a browser sends very
different headers for each. Sending XHR headers (`origin`, `csrf-token`,
`x-li-track`, `sec-fetch-mode: cors`) on what is nominally a top-level page
load is a fingerprint no real Chrome ever produces — the kind of mismatch that
gets a session flagged and revoked.

These assertions pin the two shapes apart.
"""

from __future__ import annotations

import httpx
import respx

API_URL = "https://www.linkedin.com/voyager/api/me"
PAGE_URL = "https://www.linkedin.com/in/testuser"

#: Headers a browser sends on an XHR but never on a document navigation.
XHR_ONLY = ("origin", "csrf-token", "x-li-track", "x-li-page-instance", "x-restli-protocol-version")


@respx.mock
async def test_api_requests_look_like_xhr(client) -> None:
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await client.get_json(API_URL)
    h = route.calls[0].request.headers

    assert h["sec-fetch-mode"] == "cors"
    assert h["sec-fetch-dest"] == "empty"
    assert h["x-restli-protocol-version"] == "2.0.0"
    assert h["accept"] == "application/vnd.linkedin.normalized+json+2.1"
    assert "csrf-token" in h
    assert "origin" in h


@respx.mock
async def test_page_requests_look_like_a_browser_navigation(client) -> None:
    route = respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    await client.get_html(PAGE_URL)
    h = route.calls[0].request.headers

    # A document navigation, not an XHR.
    assert h["sec-fetch-mode"] == "navigate"
    assert h["sec-fetch-dest"] == "document"
    assert h["sec-fetch-user"] == "?1"
    assert h["accept"].startswith("text/html")
    assert h["upgrade-insecure-requests"] == "1"

    # None of the XHR-only headers may appear, or the request fingerprints as
    # automation rather than as a person opening a page.
    for name in XHR_ONLY:
        assert name not in h, f"{name!r} must not be sent on a page navigation"


@respx.mock
async def test_page_requests_still_carry_the_session_cookie(client) -> None:
    """Dropping XHR headers must not drop authentication."""
    route = respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    await client.get_html(PAGE_URL)
    h = route.calls[0].request.headers

    assert "li_at=test-li-at" in h["cookie"]
    assert 'JSESSIONID="ajax:1234567890"' in h["cookie"]
