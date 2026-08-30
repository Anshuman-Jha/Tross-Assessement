#!/usr/bin/env python
"""Live reconnaissance against LinkedIn. Development tool, not part of the service.

Captures real Voyager responses so parsers can be written against *observed*
payloads rather than assumptions about them. Guessing the upstream schema and
discovering you were wrong at the end is the standard way this kind of project
fails, so this runs first.

It also answers the questions you cannot answer offline:

* Which acquisition tiers does this account actually have access to?
* What are today's GraphQL queryId hashes?
* Which profile sections come back populated?

Captured payloads are written to ``tests/fixtures/`` **after PII scrubbing**,
so they are safe to commit and to run in CI.

Usage::

    export LINKEDIN_LI_AT="..."          # from a logged-in browser
    python scripts/recon.py williamhgates
    python scripts/recon.py williamhgates --raw   # skip scrubbing (never commit)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.linkedin import endpoints
from app.linkedin.client import VoyagerClient
from app.linkedin.exceptions import LinkedInError
from app.linkedin.html_fallback import (
    extract_embedded_payloads,
    looks_like_authwall,
)
from app.linkedin.query_ids import (
    PROFILE_BY_VANITY,
    PROFILE_CARDS,
    QueryIdResolver,
)
from app.linkedin.session import LinkedInSession
from app.linkedin.session_pool import SessionPool
from app.observability.logging import configure_logging
from scripts.sanitize_fixture import sanitize

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

SECTIONS = (
    "top_card",
    "about",
    "experience",
    "education",
    "skills",
    "certifications",
    "languages",
    "projects",
    "publications",
    "honors",
    "volunteering_experience",
    "courses",
    "organizations",
    "test_scores",
)

GREEN, RED, YELLOW, DIM, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}  ✓{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{RED}  ✗{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}  !{RESET} {msg}")


def save(name: str, payload: Any, *, raw: bool) -> Path:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{name}.json"
    data = payload if raw else sanitize(payload)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("public_id", help="Profile slug, e.g. williamhgates")
    parser.add_argument(
        "--raw", action="store_true", help="Skip PII scrubbing. Never commit the output."
    )
    parser.add_argument("--no-save", action="store_true", help="Probe without writing files.")
    args = parser.parse_args()

    configure_logging("WARNING", json_output=False)

    li_at = os.environ.get("LINKEDIN_LI_AT")
    if not li_at:
        fail("LINKEDIN_LI_AT is not set.")
        print(
            "\n  Get it from a logged-in browser:\n"
            "    DevTools > Application > Cookies > https://www.linkedin.com > li_at\n"
            "\n  Then:  export LINKEDIN_LI_AT='...'\n"
        )
        return 2

    session = LinkedInSession(
        li_at=li_at, jsessionid=os.environ.get("LINKEDIN_JSESSIONID", ""), label="recon"
    )
    pool = SessionPool([session], requests_per_minute=60)
    client = VoyagerClient(pool, min_delay_ms=600, jitter_ms=500, max_retries=2)
    saved: list[str] = []

    try:
        # ---------------------------------------------------------- credential
        print(f"\n{DIM}── Credential ──{RESET}")
        try:
            me = await client.get_json(endpoints.me_url())
            ok("Session is valid (/voyager/api/me responded)")
            if not args.no_save:
                saved.append(str(save("me", me, raw=args.raw)))
        except LinkedInError as exc:
            fail(f"Session rejected: {exc}")
            print("\n  The li_at cookie is expired or invalid. Re-copy it and retry.\n")
            return 1

        # ------------------------------------------------------------ queryIds
        print(f"\n{DIM}── queryId discovery ──{RESET}")
        resolver = QueryIdResolver(path=None, discovery_enabled=True)
        await resolver.ensure_fresh(client, force=True)
        if resolver.is_usable:
            ok("Discovered every required queryId from LinkedIn's JS bundles")
            for name in (PROFILE_BY_VANITY, PROFILE_CARDS):
                print(f"      {name} = {resolver.get(name).split('.', 1)[-1]}")
            if not args.no_save:
                path = FIXTURES.parent.parent / "query_ids.json"
                path.write_text(
                    json.dumps(
                        {n: resolver.get(n) for n in resolver.snapshot()["ids"]},  # type: ignore[union-attr]
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
                ok(f"Wrote {path.name} (commit this — it is not a secret)")
        else:
            warn(f"Could not discover: {', '.join(resolver.missing)}")
            warn("The GraphQL tier will step aside; REST and HTML tiers still apply.")

        # ------------------------------------------------------- tier 1: graphql
        print(f"\n{DIM}── Tier 1: Voyager GraphQL ──{RESET}")
        profile_urn = None
        if resolver.is_usable:
            try:
                payload = await client.get_json(
                    endpoints.profile_by_vanity_url(
                        args.public_id, resolver.get(PROFILE_BY_VANITY)
                    ),
                    referer=endpoints.profile_html_url(args.public_id),
                )
                for entity in payload.get("included", []):
                    urn = entity.get("entityUrn", "")
                    if isinstance(urn, str) and "fsd_profile" in urn:
                        profile_urn = urn
                        break
                if profile_urn:
                    ok(f"Resolved profile URN: {profile_urn}")
                    if not args.no_save:
                        saved.append(str(save("graphql_vanity", payload, raw=args.raw)))
                else:
                    warn("Vanity lookup returned no profile URN")
            except LinkedInError as exc:
                fail(f"Vanity lookup failed: {exc}")

        if profile_urn:
            for section in SECTIONS:
                try:
                    payload = await client.get_json(
                        endpoints.profile_cards_url(
                            profile_urn, section, resolver.get(PROFILE_CARDS)
                        ),
                        referer=endpoints.profile_html_url(args.public_id),
                    )
                except LinkedInError as exc:
                    fail(f"{section}: {type(exc).__name__}")
                    continue
                count = len(payload.get("included", []))
                (ok if count else warn)(f"{section}: {count} entities")
                if count and not args.no_save:
                    saved.append(str(save(f"graphql_{section}", payload, raw=args.raw)))

        # ---------------------------------------------------------- tier 2: rest
        print(f"\n{DIM}── Tier 2: Voyager REST (legacy) ──{RESET}")
        for label, url in (
            ("profileView", endpoints.profile_view_url(args.public_id)),
            ("contactInfo", endpoints.profile_contact_info_url(args.public_id)),
        ):
            try:
                payload = await client.get_json(
                    url, referer=endpoints.profile_html_url(args.public_id)
                )
                ok(f"{label}: {len(payload.get('included', []))} entities")
                if not args.no_save:
                    saved.append(str(save(f"rest_{label}", payload, raw=args.raw)))
            except LinkedInError as exc:
                warn(f"{label}: {type(exc).__name__} — {exc}")

        # ---------------------------------------------------------- tier 3: html
        print(f"\n{DIM}── Tier 3: embedded HTML ──{RESET}")
        try:
            page = await client.get_html(endpoints.profile_html_url(args.public_id))
            if looks_like_authwall(page):
                fail("Served the public authwall — cookie not applied to HTML requests")
            else:
                payloads = extract_embedded_payloads(page)
                entities = sum(len(p.get("included", [])) for p in payloads)
                (ok if payloads else warn)(
                    f"{len(payloads)} embedded payloads, {entities} entities"
                )
                if payloads and not args.no_save:
                    saved.append(
                        str(save("html_embedded", {"payloads": payloads}, raw=args.raw))
                    )
        except LinkedInError as exc:
            fail(f"HTML fetch failed: {exc}")

    finally:
        await client.aclose()

    print(f"\n{DIM}── Summary ──{RESET}")
    if saved:
        ok(f"Saved {len(saved)} fixtures to tests/fixtures/")
        if args.raw:
            warn("--raw was used: these contain real PII. Do NOT commit them.")
        else:
            ok("PII scrubbed — safe to commit")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
