"""The HTTP transport that talks to LinkedIn's internal Voyager API.

No browser is involved anywhere. This is a plain HTTP/2 client that reproduces
the header set LinkedIn's own web app sends, because Voyager rejects requests
that do not look like they came from the first-party client.

Three headers carry most of the weight:

* ``csrf-token``               — the JSESSIONID value, quotes stripped.
* ``x-restli-protocol-version: 2.0.0`` — selects Rest.li 2.0 URL encoding.
* ``accept: application/vnd.linkedin.normalized+json+2.1`` — asks LinkedIn to
  return the *normalised* form, where entities are flattened into a top-level
  ``included[]`` array referenced by URN. Without it responses come back deeply
  nested and far harder to traverse. This single header removes a lot of
  parsing work.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Literal
from urllib.parse import quote, urljoin

import httpx

from app.linkedin.exceptions import (
    AuthenticationError,
    ChallengeError,
    NoHealthySessionError,
    ProfileNotFoundError,
    ProfilePrivateError,
    QueryIdError,
    RateLimitedError,
    UpstreamError,
)
from app.linkedin.session import LinkedInSession
from app.linkedin.session_pool import SessionPool
from app.observability.logging import get_logger

logger = get_logger(__name__)

LINKEDIN_HOST = "www.linkedin.com"
BASE_URL = f"https://{LINKEDIN_HOST}"
VOYAGER_BASE = f"{BASE_URL}/voyager/api"

# Pinned to a recent stable Chrome on macOS. The UA, the client-hint headers and
# the x-li-track clientVersion should stay internally consistent; a mismatched
# set is itself a fingerprint.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CLIENT_VERSION = "1.13.36160"

#: LinkedIn's nonstandard "you are being throttled / blocked" status.
HTTP_LINKEDIN_BLOCKED = 999

#: Same-host hops we will follow before declaring a loop.
MAX_REDIRECTS = 5

#: Cookies whose deletion means LinkedIn has rejected the session.
_AUTH_COOKIES = ("li_at", "liap", "li_a")


class _Redirect(Exception):  # noqa: N818 - control flow, not an error condition
    """Internal signal: follow a benign same-host redirect."""

    def __init__(self, target: str) -> None:
        super().__init__(target)
        self.target = target


def _is_linkedin_url(url: str) -> bool:
    """Whether a redirect target is still on LinkedIn.

    Guards against following a redirect off-site while carrying session
    cookies. Matches on a label boundary so ``evil-linkedin.com`` is refused.
    """
    host = (httpx.URL(url).host or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def looks_like_login_page(body: str) -> bool:
    """Whether a 200 response is actually LinkedIn's sign-in page.

    LinkedIn serves the login page with a 200 after an internal redirect, so
    the status code alone cannot distinguish "authenticated" from "please log
    in". The markers below appear on the sign-in and authwall pages and not on
    an authenticated feed or profile.
    """
    head = body[:8000].lower()
    markers = (
        "uas/login-submit",
        "id=\"username\"",
        "name=\"session_key\"",
        "sign in to linkedin",
        "authwall",
    )
    return any(m in head for m in markers)


def _session_cookies_were_deleted(response: httpx.Response) -> bool:
    """Detect LinkedIn expiring the auth cookies to invalidate a session.

    Observed against live LinkedIn: a stale ``li_at`` produces a 302 whose
    ``Set-Cookie`` headers expire ``li_at``/``liap``/``li_a`` at the epoch.
    There is no distinguishing status code, so this is the reliable signal.
    """
    for line in response.headers.get_list("set-cookie"):
        name, _, rest = line.partition("=")
        if name.strip().lower() not in _AUTH_COOKIES:
            continue
        low = rest.lower()
        if "01-jan-1970" in low or "01 jan 1970" in low or "max-age=0" in low:
            return True
    return False


def _x_li_track() -> str:
    return (
        f'{{"clientVersion":"{CLIENT_VERSION}","mpVersion":"{CLIENT_VERSION}",'
        '"osName":"web","timezoneOffset":0,"timezone":"UTC",'
        '"deviceFormFactor":"DESKTOP","mpName":"voyager-web",'
        '"displayDensity":2,"displayWidth":2560,"displayHeight":1440}'
    )


def _page_instance() -> str:
    """A fresh page-instance URN per request, as the real client emits."""
    import uuid

    return f"urn:li:page:d_flagship3_profile_view_base;{uuid.uuid4()}"


class VoyagerClient:
    """Async client for Voyager endpoints, with rate limiting and retries."""

    def __init__(
        self,
        pool: SessionPool,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        max_concurrent: int = 4,
        min_delay_ms: int = 350,
        jitter_ms: int = 400,
        cooldown_seconds: int = 900,
    ) -> None:
        self._pool = pool
        self._max_retries = max_retries
        self._min_delay = min_delay_ms / 1000.0
        self._jitter = jitter_ms / 1000.0
        self._cooldown_seconds = cooldown_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._last_request_at = 0.0
        self._pace_lock = asyncio.Lock()
        self._bootstrap_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=max_concurrent * 2, max_keepalive_connections=8),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> VoyagerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------- headers

    def _base_headers(self, session: LinkedInSession) -> dict[str, str]:
        """Headers common to both request shapes."""
        return {
            # A missing Host header makes Voyager answer 400.
            "host": LINKEDIN_HOST,
            "user-agent": USER_AGENT,
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
            "cookie": session.cookie_header(),
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        }

    def _api_headers(self, session: LinkedInSession, referer: str | None = None) -> dict[str, str]:
        """Headers for a Voyager XHR, as the Ember app issues them."""
        headers = self._base_headers(session)
        headers.update(
            {
                "accept": "application/vnd.linkedin.normalized+json+2.1",
                "referer": referer or f"{BASE_URL}/feed/",
                "origin": BASE_URL,
                "x-li-lang": "en_US",
                "x-li-track": _x_li_track(),
                "x-li-page-instance": _page_instance(),
                "x-restli-protocol-version": "2.0.0",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            }
        )
        if session.has_jsessionid:
            headers["csrf-token"] = session.csrf_token
        return headers

    def _html_headers(self, session: LinkedInSession, referer: str | None = None) -> dict[str, str]:
        """Headers for a top-level page load, as a browser navigation.

        Deliberately omits every XHR-only header — ``origin``, ``csrf-token``,
        ``x-li-track``, ``x-li-page-instance``, ``x-restli-protocol-version``
        — and uses ``sec-fetch-mode: navigate`` / ``sec-fetch-dest: document``.

        This matters: Chrome never sends CORS fetch metadata or a CSRF token on
        a document navigation. Sending them makes a page load fingerprint as a
        malformed API call, which is precisely the sort of anomaly that gets a
        session flagged and revoked rather than merely rejected.
        """
        headers = self._base_headers(session)
        headers.update(
            {
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin" if referer else "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
                "cache-control": "max-age=0",
            }
        )
        if referer:
            headers["referer"] = referer
        return headers

    # ---------------------------------------------------------------- pacing

    async def _pace(self) -> None:
        """Serialise a minimum gap plus jitter between upstream requests.

        Uniform request spacing is itself a fingerprint, so the gap is
        randomised rather than fixed.
        """
        async with self._pace_lock:
            elapsed = time.monotonic() - self._last_request_at
            delay = self._min_delay + random.uniform(0, self._jitter)  # noqa: S311
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last_request_at = time.monotonic()

    # ------------------------------------------------------------- bootstrap

    async def ensure_bootstrapped(self, session: LinkedInSession) -> None:
        """Obtain a CSRF token by loading a page, as a browser would.

        Voyager rejects an API request with no ``csrf-token``, and that token
        *is* the ``JSESSIONID`` cookie. A browser never trips over this because
        it has always loaded a page first and been issued one.

        Doing the same here has a second benefit: the JSESSIONID we get is
        guaranteed to belong to the same session as our ``li_at``. Pasting a
        mismatched pair causes ``403 CSRF check failed``, and LinkedIn responds
        to repeated CSRF failures by revoking the session outright — so
        negotiating it is safer than accepting one by configuration.
        """
        if session.has_jsessionid:
            return

        async with self._bootstrap_lock:
            if session.has_jsessionid:  # another coroutine won the race
                return

            logger.info("voyager.bootstrapping_session", session=session.label)

            # Deliberately routed through `request()` rather than httpx's own
            # redirect following. httpx applies its cookie jar on each hop with
            # `add_unredirected_header`, which *replaces* the Cookie header we
            # built — so hop 2 would go out without `li_at` and LinkedIn would
            # answer with the login page, indistinguishable from a bad cookie.
            # `request()` rebuilds headers from the session for every hop, so
            # the credential survives the chain.
            page = await self.request(
                f"{BASE_URL}/feed/",
                kind="html",
                expect_json=False,
                session=session,
            )

            if looks_like_login_page(str(page)):
                session.mark_dead("li_at did not authenticate")
                raise AuthenticationError(
                    "LinkedIn served the login page instead of an authenticated "
                    "session. The li_at cookie is not valid or has expired — "
                    "copy a fresh one from a browser that is currently logged in."
                )

            if not session.has_jsessionid:
                raise AuthenticationError(
                    "LinkedIn did not issue a JSESSIONID. The li_at cookie is "
                    "probably invalid or incomplete."
                )
            logger.info(
                "voyager.session_bootstrapped",
                session=session.label,
                cookies=sorted(session.extra_cookies),
            )

    # --------------------------------------------------------------- request

    async def request(
        self,
        url: str,
        *,
        method: Literal["GET", "POST"] = "GET",
        kind: Literal["api", "html"] = "api",
        referer: str | None = None,
        expect_json: bool = True,
        session: LinkedInSession | None = None,
    ) -> Any:
        """Perform a paced, retried, session-aware request against LinkedIn.

        Returns parsed JSON when ``expect_json``, else the response text.
        """
        attempt = 0
        last_exc: Exception | None = None
        current_url = url
        redirects = 0

        while attempt <= self._max_retries:
            attempt += 1
            try:
                active = session or await self._pool.acquire()
            except NoHealthySessionError as exc:
                # Handling a 429 typically cools down the very session we would
                # retry on. Surfacing "no healthy session" there would bury the
                # real cause, so the original error wins whenever we have one.
                if last_exc is not None:
                    raise last_exc from exc
                raise

            # API calls need a CSRF token; obtain one the way a browser does.
            if kind == "api":
                await self.ensure_bootstrapped(active)

            async with self._semaphore:
                await self._pace()
                try:
                    response = await self._client.request(
                        method,
                        current_url,
                        headers=(
                            self._api_headers(active, referer)
                            if kind == "api"
                            else self._html_headers(active, referer)
                        ),
                    )
                except httpx.TimeoutException as exc:
                    last_exc = UpstreamError(f"Timed out talking to LinkedIn: {exc}")
                    self._pool.report_error(active, "timeout")
                except httpx.TransportError as exc:
                    last_exc = UpstreamError(f"Transport error talking to LinkedIn: {exc}")
                    self._pool.report_error(active, f"transport: {type(exc).__name__}")
                else:
                    self._absorb_cookies(active, response)
                    try:
                        result = self._handle(
                            response, active, current_url, expect_json=expect_json
                        )
                    except _Redirect as hop:
                        # A benign same-host hop. It does not consume a retry,
                        # but is bounded so a redirect loop cannot spin.
                        redirects += 1
                        # A redirect back to the *same* URL is normal here:
                        # LinkedIn uses it to set routing cookies and have the
                        # client retry. The retry carries the cookies we just
                        # absorbed, so it is allowed — bounded by MAX_REDIRECTS
                        # so a genuine loop still terminates.
                        if redirects > MAX_REDIRECTS:
                            self._pool.report_error(active, "redirect loop")
                            raise UpstreamError(
                                f"LinkedIn redirect loop at {_safe_url(hop.target)}"
                            ) from None
                        logger.debug(
                            "voyager.following_redirect",
                            frm=_safe_url(current_url),
                            to=_safe_url(hop.target),
                        )
                        current_url = hop.target
                        attempt -= 1  # this hop is not a failed attempt
                        continue
                    except (RateLimitedError, UpstreamError, QueryIdError) as exc:
                        last_exc = exc
                    else:
                        self._pool.report_success(active)
                        return result

            if attempt <= self._max_retries and last_exc is not None:
                backoff = self._backoff(attempt, last_exc)
                logger.warning(
                    "voyager.retrying",
                    attempt=attempt,
                    max_attempts=self._max_retries + 1,
                    backoff_s=round(backoff, 2),
                    error=type(last_exc).__name__,
                    url=_safe_url(current_url),
                )
                await asyncio.sleep(backoff)

        assert last_exc is not None
        raise last_exc

    def _backoff(self, attempt: int, exc: Exception) -> float:
        if isinstance(exc, RateLimitedError) and exc.retry_after:
            return min(exc.retry_after, 60.0)
        # Exponential with full jitter.
        return min(2.0**attempt, 30.0) * random.uniform(0.5, 1.0)  # noqa: S311

    def _absorb_cookies(self, session: LinkedInSession, response: httpx.Response) -> None:
        """Persist every cookie LinkedIn sets onto the session.

        Not merely a nicety: `lidc` pins the session to a LinkedIn datacenter,
        and a request without it is answered with a 302 back to the same URL
        trying to set it. A client that discards cookies therefore loops
        forever on endpoints that a browser reaches on the second hop.
        """
        if not response.cookies:
            return
        session.absorb(dict(response.cookies))

    # -------------------------------------------------------------- responses

    def _handle(
        self,
        response: httpx.Response,
        session: LinkedInSession,
        url: str,
        *,
        expect_json: bool,
    ) -> Any:
        status = response.status_code

        if status == 200:
            if not expect_json:
                return response.text
            try:
                return response.json()
            except ValueError as exc:
                # A 200 carrying HTML is LinkedIn quietly serving a login wall.
                body = response.text[:400]
                if "authwall" in body.lower() or "<html" in body[:200].lower():
                    self._pool.report_error(session, "authwall", fatal=True)
                    raise AuthenticationError(
                        "LinkedIn returned a login wall instead of JSON. The li_at "
                        "cookie is expired or invalid."
                    ) from exc
                raise UpstreamError(f"LinkedIn returned non-JSON on 200: {exc}") from exc

        if status in (301, 302, 303, 307, 308):
            # A stale session is signalled by LinkedIn *deleting* the auth
            # cookies rather than by any status code, so check that first: it
            # is by far the most actionable thing we can tell an operator.
            if _session_cookies_were_deleted(response):
                self._pool.report_error(session, "li_at rejected", fatal=True)
                raise AuthenticationError(
                    "LinkedIn rejected the session and expired the li_at cookie. "
                    "The cookie is stale or revoked — copy a fresh li_at from a "
                    "browser that is currently logged in."
                )

            location = response.headers.get("location", "")
            if any(m in location for m in ("/authwall", "/login", "/uas/login", "/checkpoint")):
                fatal = "/checkpoint" in location
                self._pool.report_error(session, f"redirect to {location}", fatal=True)
                if fatal:
                    raise ChallengeError(
                        "LinkedIn redirected to a security checkpoint. The account "
                        "needs to be verified manually in a browser."
                    )
                raise AuthenticationError(
                    "LinkedIn redirected to the login wall. The li_at cookie is "
                    "expired or invalid."
                )

            # Benign same-host redirects are normal (trailing-slash
            # normalisation, datacenter routing) and are followed. Redirects
            # off linkedin.com are refused outright — following one would send
            # the session cookie to a third party.
            target = urljoin(str(response.request.url), location) if location else ""
            if target and _is_linkedin_url(target):
                raise _Redirect(target)
            raise UpstreamError(
                f"Unexpected redirect to {location!r}"
                if location
                else f"Received {status} with no Location header."
            )

        if status == 401:
            self._pool.report_error(session, "401 unauthorized", fatal=True)
            raise AuthenticationError(
                "LinkedIn rejected the session cookie (401). Refresh li_at from a "
                "logged-in browser."
            )

        if status == 403:
            body = response.text[:500].lower()
            if "csrf" in body:
                self._pool.report_error(session, "csrf rejected", fatal=True)
                raise AuthenticationError(
                    "LinkedIn rejected the CSRF token. JSESSIONID and li_at must "
                    "come from the same browser session."
                )
            # A plain 403 on a profile route usually means visibility, not auth.
            raise ProfilePrivateError(
                "LinkedIn returned 403 for this profile. It is likely private, "
                "out of network, or restricted for this account."
            )

        if status == 404:
            raise ProfileNotFoundError("LinkedIn has no profile at that URL (404).")

        if status == 400:
            body = response.text[:500]
            # Voyager answers 400 when a queryId is unknown — i.e. rotated.
            if "queryId" in body or "PARSE_ERROR" in body or "unknown query" in body.lower():
                raise QueryIdError(
                    "LinkedIn rejected the GraphQL queryId; it has likely rotated.",
                    detail={"body": body[:200]},
                )
            raise UpstreamError(f"LinkedIn returned 400: {body[:200]}")

        if status == 429 or status == HTTP_LINKEDIN_BLOCKED:
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            self._pool.report_error(
                session, f"{status} rate limited", cooldown=retry_after or self._cooldown_seconds
            )
            raise RateLimitedError(
                f"LinkedIn is throttling this account (HTTP {status}). "
                "Reduce request volume and retry later.",
                retry_after=retry_after,
            )

        if 500 <= status < 600:
            self._pool.report_error(session, f"{status} upstream")
            raise UpstreamError(f"LinkedIn returned {status}.")

        self._pool.report_error(session, f"unexpected {status}")
        raise UpstreamError(f"Unexpected status {status} from LinkedIn.")

    # ----------------------------------------------------------- convenience

    async def get_json(self, url: str, *, referer: str | None = None) -> Any:
        return await self.request(url, kind="api", referer=referer, expect_json=True)

    async def get_html(self, url: str, *, referer: str | None = None) -> str:
        result = await self.request(url, kind="html", referer=referer, expect_json=False)
        return str(result)

    async def get_raw(self, url: str) -> str:
        """Unauthenticated fetch, used for public JS bundles on the CDN."""
        response = await self._client.get(
            url, headers={"user-agent": USER_AGENT, "accept": "*/*"}
        )
        response.raise_for_status()
        return response.text


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _safe_url(url: str) -> str:
    """URLs here never carry credentials, but keep them short in logs."""
    return url[:160]


def encode_rest_li(value: str) -> str:
    """Percent-encode a value for a Rest.li 2.0 path segment."""
    return quote(value, safe="")
