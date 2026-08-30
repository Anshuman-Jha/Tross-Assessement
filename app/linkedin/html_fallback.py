"""Tier 3: recovering Voyager payloads embedded in server-rendered HTML.

LinkedIn's web app is an Ember application rendered with a BigPipe-style
streaming pattern. To avoid a render-blocking round trip, the server inlines
the API responses the page is about to need directly into the HTML::

    <code style="display:none" id="bpr-guid-1234567">
      {"data":{...},"included":[...]}
    </code>

These are byte-for-byte the same Voyager payloads the GraphQL endpoints return,
which is what makes this a genuine third acquisition path rather than a
different kind of scraping: **the existing parsers consume it unchanged**.

Two properties make it the right safety net:

* It needs **no queryId**, so it keeps working across the hash rotation that
  breaks the GraphQL tier.
* It is still a plain authenticated ``GET`` — no browser, no JavaScript
  execution, no headless anything — so it stays inside the challenge's
  "no browser" constraint.

The trade-off is that the server inlines only what the *initial* viewport
needs. Long sections arrive truncated, which is why this ranks third rather
than first.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from app.observability.logging import get_logger

logger = get_logger(__name__)

#: The inlined payload blocks. LinkedIn has used both `<code>` and `<script>`
#: wrappers and several id prefixes, so the id pattern is kept loose.
_CODE_BLOCK_RE = re.compile(
    r"<code[^>]*\bid=\"(?P<id>bpr-guid-[^\"]+)\"[^>]*>(?P<body>.*?)</code>",
    re.DOTALL | re.IGNORECASE,
)
_SCRIPT_BLOCK_RE = re.compile(
    r"<script[^>]*\btype=\"application/(?:vnd\.linkedin\.deferred\+json|json)\"[^>]*>"
    r"(?P<body>.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)

#: Signals that we were served the logged-out wall rather than the profile.
_AUTHWALL_MARKERS = (
    "authwall",
    "/uas/login",
    "please log in",
    "join linkedin",
    "sign in to view",
)


def looks_like_authwall(page: str) -> bool:
    head = page[:6000].lower()
    return any(marker in head for marker in _AUTHWALL_MARKERS)


def extract_embedded_payloads(page: str) -> list[dict[str, Any]]:
    """Return every embedded Voyager payload found in the page, in order."""
    payloads: list[dict[str, Any]] = []

    for match in _CODE_BLOCK_RE.finditer(page):
        parsed = _decode(match.group("body"))
        if parsed is not None:
            payloads.append(parsed)

    for match in _SCRIPT_BLOCK_RE.finditer(page):
        parsed = _decode(match.group("body"))
        if parsed is not None:
            payloads.append(parsed)

    logger.debug("html_fallback.payloads_extracted", count=len(payloads))
    return payloads


def _decode(body: str) -> dict[str, Any] | None:
    """Unescape and JSON-decode one inlined block.

    The bodies are HTML-escaped (``&quot;`` for every quote), and many blocks
    are metadata rather than API payloads, so failures here are expected and
    silent.
    """
    text = body.strip()
    if not text or text[0] not in "{[":
        text = html.unescape(text).strip()
    if not text or text[0] not in "{[":
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        try:
            parsed = json.loads(html.unescape(text))
        except (json.JSONDecodeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None


def merge_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine many payloads into one normalised envelope.

    The page inlines one payload per API call it is anticipating; merging their
    ``included[]`` arrays reconstructs a single entity graph that the ordinary
    parsers can read as though one request had returned it all.
    """
    included: list[dict[str, Any]] = []
    seen_urns: set[str] = set()
    data: dict[str, Any] = {}

    for payload in payloads:
        entries = payload.get("included")
        if isinstance(entries, list):
            for entity in entries:
                if not isinstance(entity, dict):
                    continue
                urn = entity.get("entityUrn")
                if isinstance(urn, str):
                    if urn in seen_urns:
                        continue
                    seen_urns.add(urn)
                included.append(entity)
        block = payload.get("data")
        if isinstance(block, dict) and not data:
            data = block

    return {"data": data, "included": included}


def payload_from_html(page: str) -> dict[str, Any]:
    """Extract and merge every embedded payload from a profile page."""
    return merge_payloads(extract_embedded_payloads(page))


def has_profile_content(payload: dict[str, Any]) -> bool:
    """Whether a merged payload actually contains profile entities.

    A page can parse cleanly and still hold nothing useful (an interstitial, a
    404 shell), so the tier checks this before declaring success.
    """
    included = payload.get("included")
    if not isinstance(included, list) or not included:
        return False
    for entity in included:
        if not isinstance(entity, dict):
            continue
        type_name = entity.get("$type", "")
        if isinstance(type_name, str) and (
            "identity.profile" in type_name or "Profile" in type_name
        ):
            return True
        if "topComponents" in entity:
            return True
    return False
