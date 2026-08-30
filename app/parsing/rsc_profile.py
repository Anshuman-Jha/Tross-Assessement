"""Parsing LinkedIn's current profile page (React Server Components).

LinkedIn has migrated profile pages off Ember. The old ``<code id="bpr-guid">``
blocks that inlined Voyager payloads are gone; so is the ``artdeco`` design
system. Pages are now React Server Components, and the page ships a Flight
payload::

    <script id="rehydrate-data">
      window.__como_rehydration__ = ["1:I[\\"64c7816b…\\",[],\\"default\\"]\\n2:I[…", …]
    </script>

Two consequences shape this module.

**Class names are hashed** (``_01e54e47``) and change every build, so selecting
on them would break within days. Everything here anchors on content and
structure instead: the Flight payload's own field names, the document title,
``aria-label`` text, and the ordering of text nodes.

**The top card is server-rendered; the rest is lazy-loaded.** Name, headline,
location, current company and images are present in the initial HTML.
Experience, education and skills are *not* — the page requests them separately
after hydration (``profile-top-card-experience-lazy-load-…``). So this parser
returns a genuine but partial profile, and the caller reports the gap through
``meta.completeness`` rather than implying the sections are empty.

Verified against a real capture of ``linkedin.com/in/williamhgates``.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

from app.models.profile import Basics, ContactInfo, Location, Profile
from app.observability.logging import get_logger
from app.parsing.images import Image, ImageArtifact

logger = get_logger(__name__)

_REHYDRATE_RE = re.compile(
    r'<script[^>]*id="rehydrate-data"[^>]*>(.*?)</script>', re.S | re.I
)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_SCRIPT_RE = re.compile(r"<script.*?</script>", re.S | re.I)
_STYLE_RE = re.compile(r"<style.*?</style>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

#: Profile photos and cover images on LinkedIn's CDN.
_IMAGE_RE = re.compile(r"https://media\.licdn\.com/dms/image/[^\"'\\\s>]+")
#: The rendered size is encoded in the path, e.g. `shrink_200_200`.
_SIZE_RE = re.compile(r"_(\d{2,4})_(\d{2,4})/")

_LOGIN_MARKERS = ("uas/login", "authwall", "session_key", "sign in to linkedin")


def looks_authenticated(page: str) -> bool:
    """Whether this page was served to a logged-in session."""
    head = page[:6000].lower()
    if any(m in head for m in _LOGIN_MARKERS):
        return False
    return "rehydrate-data" in page or "__como_rehydration__" in page


# --------------------------------------------------------------- flight payload


def extract_flight_payload(page: str) -> str:
    """Return the concatenated RSC Flight rows, or an empty string."""
    match = _REHYDRATE_RE.search(page)
    if not match:
        return ""
    body = match.group(1).strip()
    _, _, assignment = body.partition("=")
    assignment = assignment.strip().rstrip(";").strip()
    if not assignment:
        return ""
    try:
        rows = json.loads(assignment)
    except (json.JSONDecodeError, ValueError):
        # Not JSON-parseable: fall back to the raw text, which still contains
        # the field names we scan for.
        return assignment
    if isinstance(rows, list):
        return "".join(r for r in rows if isinstance(r, str))
    return str(rows)


def _field(blob: str, key: str, limit: int = 400) -> str | None:
    """First string value for ``"key":"value"`` in the payload."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]{{0,{limit}}})"', blob)
    if not match:
        return None
    value = html_lib.unescape(match.group(1)).strip()
    return value or None


def _profile_urn(blob: str) -> str | None:
    """The subject's profile URN.

    LinkedIn sometimes double-prefixes this (``urn:li:fsd_profile:urn:li:
    fsd_profile:ACoAA…``), so the trailing member id is taken and rebuilt.
    """
    match = re.search(
        r"urn:li:fsd_profile:(?:urn:li:fsd_profile:)?([A-Za-z0-9_-]+)", blob
    )
    return f"urn:li:fsd_profile:{match.group(1)}" if match else None


# ----------------------------------------------------------------------- images


def _images(page: str) -> tuple[Image | None, Image | None]:
    """Recover the profile photo and cover image.

    LinkedIn emits the same photo at several widths; they are grouped by their
    CDN asset id so each image keeps all of its variants.
    """
    by_asset: dict[str, dict[str, ImageArtifact]] = {}
    kinds: dict[str, str] = {}

    for url in _IMAGE_RE.findall(page):
        url = html_lib.unescape(url)
        size = _SIZE_RE.search(url)
        if not size:
            continue  # a bare root URL with no rendered size
        asset = url.split("/dms/image/")[1].split("/")[1] if "/dms/image/" in url else url
        width, height = int(size.group(1)), int(size.group(2))
        by_asset.setdefault(asset, {})[f"{width}x{height}"] = ImageArtifact(
            url=url, width=width, height=height
        )
        low = url.lower()
        if "displayphoto" in low:
            kinds[asset] = "photo"
        elif "backgroundimage" in low or "cover" in low:
            kinds[asset] = "cover"

    def build(asset: str) -> Image:
        arts = sorted(by_asset[asset].values(), key=lambda a: a.width or 0)
        best = arts[-1]
        return Image(url=best.url, width=best.width, height=best.height, artifacts=arts)

    photo = next((build(a) for a, k in kinds.items() if k == "photo"), None)
    cover = next((build(a) for a, k in kinds.items() if k == "cover"), None)
    return photo, cover


# ------------------------------------------------------------------ visible text


def visible_text(page: str) -> list[str]:
    """The page's text nodes in document order, scripts and styles removed."""
    body = _STYLE_RE.sub(" ", _SCRIPT_RE.sub(" ", page))
    body = _TAG_RE.sub("\n", body)
    out: list[str] = []
    for raw in body.split("\n"):
        # \xa0 is deliberate: LinkedIn renders &nbsp; throughout its markup,
        # and leaving it in breaks equality checks against visible text.
        text = html_lib.unescape(raw).replace("\xa0", " ").strip()
        if text:
            out.append(re.sub(r"\s+", " ", text))
    return out


def _headline_and_location(
    lines: list[str], full_name: str | None
) -> tuple[str | None, str | None, str | None]:
    """Read headline, current company and location from the top card.

    Anchored on the subject's own name rather than on class names, which are
    build-hashed. In the rendered order the name is followed by the headline,
    then the current company, then the location — and "Contact info" reliably
    terminates the block.
    """
    if not full_name:
        return None, None, None

    # Prefer the *last* occurrence of the bare name: earlier ones are the nav
    # bar and the document title.
    idxs = [i for i, ln in enumerate(lines) if ln == full_name]
    if not idxs:
        return None, None, None
    start = idxs[-1]

    tail = lines[start + 1 : start + 9]
    stop = next(
        (i for i, ln in enumerate(tail) if ln.lower().startswith("contact info")),
        len(tail),
    )
    block = [ln for ln in tail[:stop] if ln and ln != full_name]

    headline = block[0] if block else None
    remainder = block[1:]

    # A location line contains a comma and no sentence punctuation; the company
    # is whatever precedes it.
    location = next(
        (
            ln
            for ln in remainder
            if "," in ln and len(ln) < 90 and not ln.endswith((".", "!", "?"))
        ),
        None,
    )
    company = None
    if location and location in remainder:
        before = remainder[: remainder.index(location)]
        company = before[-1] if before else None
    elif remainder:
        company = remainder[0]

    return headline, company, location


def _websites(lines: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for ln in lines:
        for url in re.findall(r"https?://[^\s\"'<>]+", ln):
            if "linkedin.com" not in url and "licdn.com" not in url:
                seen.setdefault(url.rstrip(".,"), None)
    return list(seen)[:5]


# --------------------------------------------------------------------- entrypoint


def parse_rsc_profile(page: str, *, public_id: str | None = None) -> Profile:
    """Build a :class:`Profile` from a server-rendered LinkedIn profile page."""
    blob = extract_flight_payload(page)
    lines = visible_text(page)

    first = _field(blob, "firstName")
    last = _field(blob, "lastName")
    vanity = _field(blob, "inviteeVanityName") or public_id

    full_name = " ".join(p for p in (first, last) if p) or None
    if not full_name:
        title = _TITLE_RE.search(page)
        if title:
            candidate = html_lib.unescape(title.group(1)).split("|")[0].strip()
            full_name = candidate or None

    headline, company, location = _headline_and_location(lines, full_name)
    photo, cover = _images(page)

    basics = Basics(
        first_name=first,
        last_name=last,
        full_name=full_name,
        headline=headline,
        public_identifier=vanity,
        profile_url=_field(blob, "profileCanonicalUrl")
        or (f"https://www.linkedin.com/in/{vanity}" if vanity else None),
        location=Location(
            full=location,
            city=location.split(",")[0].strip() if location else None,
            country=location.split(",")[-1].strip() if location and "," in location else None,
        ),
        profile_picture=photo,
        background_image=cover,
        contact=ContactInfo(websites=_websites(lines)),
    )

    profile = Profile(basics=basics)
    logger.info(
        "rsc.parsed",
        public_id=vanity,
        has_name=bool(full_name),
        has_headline=bool(headline),
        has_location=bool(location),
        has_photo=photo is not None,
        current_company=bool(company),
    )
    return profile


def profile_urn_from_page(page: str) -> str | None:
    return _profile_urn(extract_flight_payload(page) or page)


def sections_are_lazy_loaded(page: str) -> bool:
    """Whether the page defers experience/education to a later request.

    When true the absence of those sections is a property of the page, not a
    parsing failure, and the caller should say so rather than reporting them
    as genuinely empty.
    """
    return "lazy-load" in page and "profile-top-card" in page


def _as_dict(value: Any) -> dict[str, Any]:  # pragma: no cover - defensive
    return value if isinstance(value, dict) else {}
