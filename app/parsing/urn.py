"""Helpers for LinkedIn URNs.

A URN looks like ``urn:li:fsd_profile:ACoAAABcDeFgHi`` or, for the older
namespace, ``urn:li:fs_miniProfile:ACoAAA...``. Voyager responses are a flat
entity graph: objects reference each other by URN rather than nesting, so
resolving them is most of the work of reading a response.
"""

from __future__ import annotations

import re

_URN_RE = re.compile(r"^urn:li:(?P<type>[a-zA-Z0-9_]+):(?P<id>.+)$")

#: Company/school pages share the numeric id space used by these URN types.
_COMPANY_TYPES = frozenset({"fsd_company", "fs_miniCompany", "company", "organization"})
_SCHOOL_TYPES = frozenset({"fsd_school", "fs_miniSchool", "school"})


def parse_urn(urn: str | None) -> tuple[str, str] | None:
    """Split a URN into ``(type, id)``, or return ``None`` if it is not one."""
    if not urn or not isinstance(urn, str):
        return None
    match = _URN_RE.match(urn.strip())
    if not match:
        return None
    return match.group("type"), match.group("id")


def urn_type(urn: str | None) -> str | None:
    parsed = parse_urn(urn)
    return parsed[0] if parsed else None


def urn_id(urn: str | None) -> str | None:
    parsed = parse_urn(urn)
    return parsed[1] if parsed else None


def is_profile_urn(urn: str | None) -> bool:
    return urn_type(urn) in {"fsd_profile", "fs_miniProfile", "fs_profile"}


def normalize_profile_urn(value: str | None) -> str | None:
    """Coerce a bare profile id or any profile URN flavour to ``fsd_profile``.

    The GraphQL endpoints expect ``urn:li:fsd_profile:...`` specifically, while
    responses may quote the legacy ``fs_miniProfile`` form for the same person.
    """
    if not value:
        return None
    parsed = parse_urn(value)
    if parsed is None:
        # A bare id like "ACoAAA..." — wrap it.
        cleaned = value.strip()
        return f"urn:li:fsd_profile:{cleaned}" if cleaned else None
    _, ident = parsed
    return f"urn:li:fsd_profile:{ident}"


def company_url_from_urn(urn: str | None) -> str | None:
    parsed = parse_urn(urn)
    if not parsed:
        return None
    kind, ident = parsed
    if kind in _COMPANY_TYPES:
        return f"https://www.linkedin.com/company/{ident}/"
    if kind in _SCHOOL_TYPES:
        return f"https://www.linkedin.com/school/{ident}/"
    return None
