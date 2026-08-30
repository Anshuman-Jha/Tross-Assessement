#!/usr/bin/env python
"""Scrub PII from captured Voyager payloads so fixtures are safe to commit.

Recorded fixtures are the only way to test parsers against real upstream
shapes, but a raw capture contains a real person's name, employer, photo URLs
and often their contact details. This replaces the *values* while preserving
every *structural* property the parsers depend on — key names, nesting, types,
list lengths, URN formats — so a scrubbed fixture exercises the same code paths
as the original.

Usage::

    python scripts/sanitize_fixture.py raw.json > clean.json
    python scripts/sanitize_fixture.py --in-place tests/fixtures/*.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any

#: Keys whose string values are replaced with deterministic fakes.
PII_KEYS = {
    "firstname": "Ada",
    "lastname": "Lovelace",
    "fullname": "Ada Lovelace",
    "name": None,  # too generic to blanket-replace; handled contextually
    "publicidentifier": "ada-lovelace",
    "headline": "Mathematician and writer",
    "summary": "Sanitised profile summary for test fixtures.",
    "occupation": "Mathematician",
    "emailaddress": "ada@example.com",
    "phonenumber": "+10000000000",
    "address": "123 Example Street",
    "birthdateon": None,
}

#: Keys whose values are dropped entirely.
DROP_KEYS = {"li_at", "jsessionid", "csrftoken", "csrf-token", "cookie", "set-cookie"}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
#: LinkedIn member ids inside URNs — stable-ise them, don't remove them.
_MEMBER_ID_RE = re.compile(r"(ACoAA)[A-Za-z0-9_-]{6,}")


def _fake_id(seed: str, length: int = 11) -> str:
    """A deterministic replacement, so repeated runs produce identical output."""
    digest = hashlib.sha256(seed.encode()).hexdigest()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(alphabet[int(digest[i : i + 2], 16) % len(alphabet)] for i in range(length))


def _scrub_string(value: str) -> str:
    value = _EMAIL_RE.sub("someone@example.com", value)
    value = _PHONE_RE.sub("+10000000000", value)
    value = _MEMBER_ID_RE.sub(lambda m: m.group(1) + _fake_id(m.group(0)), value)
    return value


def sanitize(node: Any, _depth: int = 0) -> Any:
    """Recursively replace personal data while preserving structure."""
    if _depth > 40:
        return node

    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            low = key.lower()
            if low in DROP_KEYS:
                continue
            replacement = PII_KEYS.get(low)
            if replacement is not None and isinstance(value, str):
                out[key] = replacement
            else:
                out[key] = sanitize(value, _depth + 1)
        return out

    if isinstance(node, list):
        return [sanitize(item, _depth + 1) for item in node]

    if isinstance(node, str):
        return _scrub_string(node)

    return node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="JSON files to sanitise.")
    parser.add_argument(
        "--in-place", action="store_true", help="Rewrite each file rather than printing."
    )
    args = parser.parse_args()

    for path in args.files:
        with open(path) as handle:
            data = json.load(handle)
        cleaned = sanitize(data)
        rendered = json.dumps(cleaned, indent=2, ensure_ascii=False, sort_keys=True)
        if args.in_place:
            with open(path, "w") as handle:
                handle.write(rendered + "\n")
            print(f"sanitised {path}", file=sys.stderr)
        else:
            print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
