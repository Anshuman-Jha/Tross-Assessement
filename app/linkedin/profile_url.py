"""Normalising whatever the caller passes us into a LinkedIn public identifier.

Callers paste URLs in many shapes: regional subdomains, tracking query strings,
percent-encoded unicode slugs, mobile links, or just the bare slug. All of them
have to collapse to the same cache key, or the cache silently misses and we hit
LinkedIn far more often than necessary.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from app.linkedin.exceptions import InvalidProfileUrlError

#: LinkedIn serves profiles from regional subdomains (uk., de., www., m., ...),
#: so subdomains are allowed — but the match must be on a label boundary.
#: A bare ``endswith("linkedin.com")`` would also accept ``evil-linkedin.com``.
_ALLOWED_HOSTS = frozenset({"linkedin.com"})


def _is_linkedin_host(host: str) -> bool:
    return host in _ALLOWED_HOSTS or any(host.endswith(f".{h}") for h in _ALLOWED_HOSTS)

#: `/in/<slug>` is the public profile path. Slugs allow unicode letters, digits
#: and hyphens; LinkedIn also appends a disambiguating hex suffix on collisions.
_PROFILE_PATH_RE = re.compile(r"^/(?:[a-z]{2}/)?in/(?P<slug>[^/?#]+)/?", re.IGNORECASE)

#: A bare slug typed without any URL wrapper.
_BARE_SLUG_RE = re.compile(r"^[\w\-À-￿%]{1,150}$")

#: Reserved paths that look like slugs but are not profiles.
_RESERVED = frozenset(
    {"feed", "jobs", "company", "school", "groups", "learning", "posts", "pulse", "mynetwork"}
)


def extract_public_identifier(raw: str) -> str:
    """Return the canonical public identifier for a profile URL or bare slug.

    Raises:
        InvalidProfileUrlError: if the input is not a LinkedIn *profile* reference.
    """
    if not raw or not raw.strip():
        raise InvalidProfileUrlError("Profile URL is empty.")

    candidate = raw.strip()

    # Bare slug shortcut: no scheme, no dots, no slashes.
    if "/" not in candidate and "." not in candidate:
        if not _BARE_SLUG_RE.match(candidate):
            raise InvalidProfileUrlError(f"{candidate!r} is not a valid LinkedIn profile slug.")
        return _clean_slug(candidate)

    # urlparse needs a scheme to populate netloc rather than path.
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise InvalidProfileUrlError(f"Could not parse {raw!r} as a URL.") from exc

    host = (parsed.hostname or "").lower()
    if not _is_linkedin_host(host):
        raise InvalidProfileUrlError(
            f"{host or raw!r} is not a linkedin.com host. "
            "Expected something like https://www.linkedin.com/in/<slug>."
        )

    match = _PROFILE_PATH_RE.match(parsed.path)
    if not match:
        hint = ""
        first_segment = parsed.path.strip("/").split("/")[0].lower()
        if first_segment in _RESERVED:
            hint = f" This looks like a /{first_segment} URL, not a personal profile."
        raise InvalidProfileUrlError(
            f"URL path {parsed.path!r} is not a LinkedIn profile path.{hint} "
            "Expected /in/<slug>."
        )

    return _clean_slug(match.group("slug"))


def _clean_slug(slug: str) -> str:
    """Percent-decode and strip trailing punctuation from a slug."""
    decoded = unquote(slug).strip()
    # Guard against a decoded value smuggling in a path separator.
    decoded = decoded.split("/")[0].split("?")[0].split("#")[0]
    decoded = decoded.rstrip(".,")
    if not decoded:
        raise InvalidProfileUrlError(f"{slug!r} decoded to an empty profile slug.")
    if decoded.lower() in _RESERVED:
        raise InvalidProfileUrlError(f"{decoded!r} is a reserved LinkedIn path, not a profile.")
    return decoded


def canonical_profile_url(public_id: str) -> str:
    """The canonical https URL we echo back for a given public identifier."""
    return f"https://www.linkedin.com/in/{public_id}"
