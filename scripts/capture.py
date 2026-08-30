#!/usr/bin/env python
"""Grab everything in one fast pass, before LinkedIn can revoke the session.

`recon.py` is the thorough diagnostic. This is the opposite: the minimum number
of requests needed to capture raw artefacts to disk, run back-to-back with no
pauses, because a fresh session may only survive a few minutes.

Nothing is parsed here. Raw bytes are written to `captures/` and analysed
offline afterwards, so a revoked session costs you nothing once this has run.

Usage::

    export LINKEDIN_LI_AT="..."
    python scripts/capture.py williamhgates
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "captures"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

NAV = {
    "user-agent": UA,
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "accept-language": "en-US,en;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


def api_headers(csrf: str, referer: str) -> dict[str, str]:
    return {
        "user-agent": UA,
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "accept-language": "en-US,en;q=0.9",
        "csrf-token": csrf,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "referer": referer,
        "origin": "https://www.linkedin.com",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def save(name: str, content: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(content, encoding="utf-8")
    print(f"    saved {path.relative_to(ROOT)}  ({len(content):,} bytes)")


async def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "williamhgates"
    li_at = os.environ.get("LINKEDIN_LI_AT")
    if not li_at:
        print("LINKEDIN_LI_AT is not set.")
        return 2

    started = time.monotonic()

    # The cookie goes in the jar, not a manual header: httpx replaces the
    # Cookie header when it follows a redirect, which would drop li_at midway.
    async with httpx.AsyncClient(http2=True, follow_redirects=True, timeout=30) as c:
        c.cookies.set("li_at", li_at, domain=".linkedin.com")

        print("\n[1/4] bootstrap: loading the feed")
        r = await c.get("https://www.linkedin.com/feed/", headers=NAV)
        if "/uas/login" in str(r.url) or "/authwall" in str(r.url):
            print("    FAILED — LinkedIn served the login page. Cookie is not valid.")
            return 1
        csrf = c.cookies.get("JSESSIONID", "").strip('"')
        print(f"    ok  status={r.status_code}  csrf={csrf[:24]}")
        save("feed.html", r.text)

        print("\n[2/4] identity check: /voyager/api/me")
        referer = f"https://www.linkedin.com/in/{slug}"
        me = await c.get(
            "https://www.linkedin.com/voyager/api/me",
            headers=api_headers(csrf, "https://www.linkedin.com/feed/"),
        )
        print(f"    status={me.status_code}")
        if me.status_code == 200:
            save("me.json", me.text)
            who = [e for e in me.json().get("included", []) if "firstName" in e]
            if who:
                print(f"    logged in as: {who[0].get('firstName')} {who[0].get('lastName')}")

        print(f"\n[3/4] profile page: /in/{slug}")
        page = await c.get(f"https://www.linkedin.com/in/{slug}", headers=NAV)
        print(f"    status={page.status_code}  len={len(page.text):,}")
        save(f"profile_{slug}.html", page.text)

        print("\n[4/4] legacy REST endpoints")
        for label, url in (
            ("profileView", f"https://www.linkedin.com/voyager/api/identity/profiles/{slug}/profileView"),
            ("contactInfo", f"https://www.linkedin.com/voyager/api/identity/profiles/{slug}/profileContactInfo"),
        ):
            try:
                resp = await c.get(url, headers=api_headers(csrf, referer))
                print(f"    {label}: status={resp.status_code} len={len(resp.text):,}")
                if resp.status_code == 200:
                    save(f"rest_{label}.json", resp.text)
            except httpx.HTTPError as exc:
                print(f"    {label}: {type(exc).__name__}")

        # JS bundles are public CDN assets and cost the session nothing.
        print("\n[bonus] scanning JS bundles for queryId hashes")
        bundles = sorted(set(re.findall(r"https://static\.licdn\.com/[^\"'\s>]+\.js", r.text)))
        print(f"    {len(bundles)} bundles referenced by the feed")
        found: dict[str, str] = {}
        for i in range(0, len(bundles), 6):
            chunk = bundles[i : i + 6]
            results = await asyncio.gather(
                *(c.get(u, headers={"user-agent": UA}) for u in chunk),
                return_exceptions=True,
            )
            for res in results:
                if isinstance(res, httpx.Response):
                    for n, h in re.findall(r"(voyager[A-Za-z0-9]+)\.([0-9a-f]{32})", res.text):
                        found[n] = f"{n}.{h}"
        print(f"    queryIds found: {len(found)}")
        if found:
            save("query_ids_found.json", json.dumps(found, indent=2, sort_keys=True))
            for n in sorted(found)[:10]:
                print(f"      {n}")

    print(f"\nDone in {time.monotonic() - started:.1f}s. Artefacts in captures/")
    print("These contain real profile data — they are gitignored, do not commit them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
