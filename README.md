# LinkedIn Profile API

A hosted HTTPS API that takes a LinkedIn profile URL and returns the profile as
structured JSON — name, headline, location, about, experience, education,
skills, certifications, languages, images and more.

It works by calling LinkedIn's **private internal API (Voyager)** directly over
HTTP. There is no browser, no headless Chrome, no Playwright/Selenium/Puppeteer
and no JavaScript execution anywhere in the stack.

```bash
curl -H "X-API-Key: $KEY" \
  "https://<your-deployment>/api/v1/profile?url=https://www.linkedin.com/in/williamhgates/"
```

---

## Contents

- [Quick start](#quick-start)
- [Getting a LinkedIn cookie](#getting-a-linkedin-cookie)
- [API documentation](#api-documentation)
- [Response schema](#response-schema)
- [Approach: how the reverse engineering works](#approach-how-the-reverse-engineering-works)
- [Architecture](#architecture)
- [Deployment](#deployment)
- [Development](#development)
- [Known limitations](#known-limitations)
- [Legal and responsible use](#legal-and-responsible-use)

---

## Quick start

```bash
git clone <your-repo-url>
cd linkedin-profile-api

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env               # then add your li_at — see below
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/docs** for interactive documentation.

Verify your credential and see which acquisition tiers your account can reach:

```bash
set -a && . ./.env && set +a
python scripts/recon.py williamhgates --no-save
```

### With Docker

```bash
cp .env.example .env               # add your li_at
docker compose up --build
```

Brings up the API on `:8000` plus Redis for shared caching.

---

## Getting a LinkedIn cookie

The service authenticates as a normal logged-in LinkedIn session. It needs one
value: the **`li_at`** cookie.

1. Log in to LinkedIn in a browser.
2. `F12` → **Application** → **Storage → Cookies → `https://www.linkedin.com`**
3. Copy the value of **`li_at`** (~150 characters, begins `AQEDA…`).
4. Put it in `.env`:

```dotenv
LINKEDIN_LI_AT=AQEDAxxxxxxxxxxxxxxxxxx
LINKEDIN_JSESSIONID=            # leave empty — see below
```

### Leave `JSESSIONID` empty

LinkedIn's CSRF scheme reuses the `JSESSIONID` cookie as the CSRF token, and it
**must belong to the same login session as `li_at`**. Pasting a `JSESSIONID`
from an older session produces `403 CSRF check failed`, and LinkedIn responds to
repeated CSRF failures by **revoking the session entirely** — logging you out of
your own browser.

So the service negotiates its own. Supply `li_at` alone and the client accepts
whatever `JSESSIONID` LinkedIn issues on the first request, guaranteeing a
matched pair. Only set `LINKEDIN_JSESSIONID` if you copied both values from the
same browser window at the same moment.

### Cookie hygiene

| Do | Don't |
|---|---|
| Use a **throwaway account** | Use your primary account |
| Keep that browser session logged in | Click "Sign out" — it invalidates the cookie you copied |
| Store the cookie in `.env` (gitignored) | Commit it, or paste it into an issue |

Signing out of the browser kills the copied `li_at` immediately. Closing the tab
is fine; logging out is not.

### Multiple accounts

Rotate across several sessions to spread load:

```dotenv
LINKEDIN_ACCOUNTS_JSON=[{"li_at":"AQED...","label":"acct-1"},{"li_at":"AQED...","label":"acct-2"}]
```

The pool selects least-recently-used, enforces a per-session request budget, and
quarantines any session LinkedIn rejects.

---

## API documentation

Every endpoint is also documented interactively at `/docs` (OpenAPI 3.1).

### Authentication

Send your key in the `X-API-Key` header. Configure keys via `API_KEYS`
(comma-separated). For local development set `REQUIRE_API_KEY=false`.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # generate one
```

### `GET /api/v1/profile`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | string, **required** | — | Profile URL or bare public identifier |
| `format` | `nested` \| `flat` | `nested` | Response projection |
| `refresh` | boolean | `false` | Bypass the cache and re-fetch |

```bash
curl -H "X-API-Key: $KEY" \
  "http://localhost:8000/api/v1/profile?url=https://www.linkedin.com/in/williamhgates/"
```

Any URL shape works — they all normalise to the same profile and the same cache
entry:

```
https://www.linkedin.com/in/williamhgates/
https://uk.linkedin.com/in/williamhgates?originalSubdomain=uk
linkedin.com/in/williamhgates
williamhgates
```

### `POST /api/v1/profile`

```bash
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url":"https://www.linkedin.com/in/williamhgates/"}' \
  http://localhost:8000/api/v1/profile
```

### `GET /api/v1/profile/{public_id}`

```bash
curl -H "X-API-Key: $KEY" http://localhost:8000/api/v1/profile/williamhgates
```

### Operational endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness. Always 200 while the process runs. |
| `GET /readyz` | Readiness. 503 when no usable LinkedIn session. Reports session health and queryId status — never cookie values. |
| `GET /metrics` | Prometheus metrics. |
| `GET /docs` | Interactive OpenAPI documentation. |

`/healthz` and `/readyz` are deliberately separate: an expired cookie makes the
service *unready*, not *dead*, so your platform reports the problem instead of
restart-looping a container that is working fine.

### Errors

Every failure returns the same envelope:

```json
{
  "success": false,
  "error": "PROFILE_NOT_FOUND",
  "message": "LinkedIn has no profile at that URL (404).",
  "detail": {},
  "request_id": "9c1014b3ef6f4f3e"
}
```

`error` is a stable machine-readable code; `message` is for humans and may be
reworded.

| Code | HTTP | Meaning |
|---|---|---|
| `INVALID_PROFILE_URL` | 422 | Not a LinkedIn `/in/` profile URL |
| `VALIDATION_ERROR` | 422 | Malformed request |
| `UNAUTHORIZED` | 401 | Missing or invalid `X-API-Key` |
| `PROFILE_NOT_FOUND` | 404 | No such profile |
| `PROFILE_PRIVATE` | 403 | Private, or out of network for this account |
| `TOO_MANY_REQUESTS` | 429 | This API's own rate limit |
| `RATE_LIMITED` | 429 | LinkedIn is throttling us (`Retry-After` set) |
| `SESSION_INVALID` | 502 | Cookie expired or revoked |
| `LINKEDIN_CHALLENGE` | 502 | CAPTCHA/2FA checkpoint — verify in a browser |
| `NO_HEALTHY_SESSION` | 503 | No usable session configured |
| `QUERY_ID_INVALID` | 503 | GraphQL ids rotated; re-discovery triggered |
| `ALL_TIERS_FAILED` | 502 | All three acquisition paths failed |

---

## Response schema

The challenge left the schema open. Two projections are offered.

### `format=nested` (default)

Three design rules:

1. **Stable keys.** Every section is always present. No certifications yields
   `"certifications": []`, never a missing key — consumers never need defensive
   lookups.
2. **Honest nulls.** A field is `null` when LinkedIn did not give it to us.
   Nothing is inferred or defaulted.
3. **Provenance in the envelope.** `meta.source` names which tier answered and
   `meta.completeness` flags which sections actually populated, so a thin result
   is *visibly* thin rather than silently passing for complete.

```jsonc
{
  "success": true,
  "meta": {
    "profile_url": "https://www.linkedin.com/in/williamhgates",
    "public_identifier": "williamhgates",
    "profile_urn": "urn:li:fsd_profile:ACoAAA…",
    "fetched_at": "2026-08-31T09:15:04.221Z",
    "source": "voyager_graphql",      // which tier answered
    "cached": false,
    "duration_ms": 1840,
    "completeness": { "experience": true, "education": true, "skills": false, … }
  },
  "profile": {
    "basics": {
      "first_name": "Bill", "last_name": "Gates", "full_name": "Bill Gates",
      "headline": "Co-chair, Bill & Melinda Gates Foundation",
      "about": "Sharing things I'm learning through my foundation work…",
      "location": { "full": "Seattle, Washington, United States",
                    "city": "Seattle", "country": "United States" },
      "industry": "Philanthropy",
      "connections": 500, "followers": 35000000,
      "is_premium": null, "is_influencer": true, "is_open_to_work": null,
      "profile_picture": {
        "url": "https://media.licdn.com/dms/image/…/800_800/…",
        "width": 800, "height": 800,
        "artifacts": [ { "url": "…/100_100/…", "width": 100, "height": 100 }, … ]
      },
      "background_image": { … },
      "contact": { "websites": [], "emails": [], "phones": [], "twitter": [] }
    },

    "experience": [{
      "title": "Co-chair", "company": "Bill & Melinda Gates Foundation",
      "company_url": "https://www.linkedin.com/company/1441/",
      "company_urn": "urn:li:fsd_company:1441",
      "company_logo": { "url": "…", "width": 400, "artifacts": [ … ] },
      "employment_type": "Full-time",
      "location": "Seattle, Washington", "location_type": "Hybrid",
      "dates": {
        "start": { "year": 2000, "month": 1 }, "end": null,
        "is_current": true, "duration": "26 yrs", "duration_months": 308
      },
      "is_current": true,
      "description": "…", "skills": []
    }],

    "education": [{
      "school": "Harvard University", "school_url": "…", "school_logo": { … },
      "degree": "Bachelor of Science - BS", "field_of_study": "Computer Science",
      "grade": null, "activities": null,
      "dates": { "start": { "year": 1973 }, "end": { "year": 1975 }, … },
      "description": null
    }],

    "skills":         [{ "name": "Philanthropy", "endorsement_count": 42, "insights": [] }],
    "certifications": [{ "name": "…", "issuer": "…", "issue_date": "Jan 2023",
                         "expiration_date": null, "credential_id": "ABC-123",
                         "credential_url": "https://…" }],
    "languages":      [{ "name": "English", "proficiency": "Native or bilingual" }],

    "projects": [], "publications": [], "honors": [], "volunteering": [],
    "courses": [], "patents": [], "organizations": [], "test_scores": [],
    "recommendations": { "received": [], "given": [] }
  },
  "warnings": []
}
```

### `format=flat`

The challenge cites [PhantomBuster's LinkedIn Profile Scraper][pb], whose output
is a flat, spreadsheet-shaped record. `format=flat` produces that: one level,
repeated sections unrolled into numbered columns. It drops straight into a CSV,
a CRM import or a dataframe.

[pb]: https://phantombuster.com/automations/linkedin/5589386912058181/linkedin-profile-scraper

```bash
curl -H "X-API-Key: $KEY" \
  "http://localhost:8000/api/v1/profile?url=williamhgates&format=flat"
```

```jsonc
{
  "profileUrl": "https://www.linkedin.com/in/williamhgates",
  "firstName": "Bill", "lastName": "Gates", "fullName": "Bill Gates",
  "headline": "Co-chair, Bill & Melinda Gates Foundation",
  "location": "Seattle, Washington, United States",
  "profileImageUrl": "https://media.licdn.com/…",
  "followersCount": 35000000, "connectionsCount": 500,

  "company": "Bill & Melinda Gates Foundation", "jobTitle": "Co-chair",
  "jobDateRange": "2000-01 - Present", "jobLocation": "Seattle, Washington",
  "company2": "Microsoft", "jobTitle2": "Co-founder",

  "school": "Harvard University", "schoolDegree": "Bachelor of Science - BS",
  "skill1": "Philanthropy", "skill2": "Public Speaking",
  "allSkills": "Philanthropy, Public Speaking, …",

  "experienceCount": 4, "educationCount": 1,
  "timestamp": "2026-08-31T09:15:04.221Z", "source": "voyager_graphql"
}
```

Flattening is lossy — nested dates, image variants and per-role descriptions
cannot survive it — which is why both are offered. The flat form is a *view*
derived from the nested model at response time, so the two cannot drift apart.

---

## Approach: how the reverse engineering works

### What Voyager is

LinkedIn's website is an Ember.js single-page app. Loading
`linkedin.com/in/someone` returns a near-empty HTML shell; JavaScript then calls
LinkedIn's private internal API to fetch the data and renders it:

```
Browser  ──GET /in/someone──▶  HTML shell (no profile data)
         ──XHR──────────────▶  /voyager/api/graphql?…       ← Voyager
                                {"data":…, "included":[…]}
         ◀─────────────────── JavaScript renders the page
```

`/voyager/api/` is that private API. It is undocumented, unversioned, carries no
stability guarantee, and authenticates with nothing more than your session
cookie. This service skips the browser and calls it directly.

LinkedIn's *public* developer API is not an alternative: it requires partner
approval and cannot read arbitrary profiles.

### The four things you have to get right

**1. Authentication.** Cookie `li_at` carries the session. LinkedIn's CSRF
scheme is unusual and easy to get wrong: the `JSESSIONID` cookie value *is* the
CSRF token. LinkedIn sets it with literal quotes (`JSESSIONID="ajax:123"`), the
cookie must be sent **with** them, and the `csrf-token` header must carry the
same value **without** them.

**2. Rest.li 2.0 encoding.** Voyager's GraphQL gateway does not accept a JSON
variables body. Arguments are serialised into the query string in LinkedIn's
Rest.li dialect, where only the *values* are percent-encoded:

```
/voyager/api/graphql
  ?variables=(profileUrn:urn%3Ali%3Afsd_profile%3AACoAAA…,sectionType:experience)
  &queryId=voyagerIdentityDashProfileCards.<32-hex>
```

Encoding the parentheses or colons breaks the parse. This is the second most
common cause of an unexplained `400`.

**3. `queryId` hashes — the fragile part.** Every GraphQL call must name a
pre-registered query by an opaque hash. LinkedIn generates these at build time,
ships them inside its JS bundles, and **rotates them on deploy**. Hardcoding
them is why most published LinkedIn scrapers work for a few months and then
quietly return empty results.

So resolution is a three-tier chain (`app/linkedin/query_ids.py`):

1. **Operator override** — `query_ids.json`, correctable without a redeploy.
2. **Runtime discovery** — fetch a LinkedIn page, extract the `static.licdn.com`
   bundle URLs, fetch those bundles, and regex the ids out of the JavaScript.
   This is what lets the service heal itself.
3. **Bundled defaults** — deliberately empty. A plausible-but-wrong hash yields
   a confusing `400`; an absent one fails loudly and routes to discovery.

When LinkedIn rejects an id, the entry is invalidated, discovery re-runs, and the
request retries once.

**4. The normalised response format.** Requesting
`accept: application/vnd.linkedin.normalized+json+2.1` makes LinkedIn flatten
the object graph: entities are hoisted into a top-level `included[]` array keyed
by `entityUrn`, and references become URN strings marked with a `*` prefix.

```jsonc
{
  "data": { "*elements": ["urn:li:fsd_profile:ABC"] },
  "included": [ { "entityUrn": "urn:li:fsd_profile:ABC", "firstName": "Ada" } ]
}
```

`app/parsing/collection.py` indexes that graph and dereferences lazily with a
depth bound — the graph contains genuine cycles (a profile references a position
that references the profile), so eager expansion either loops or explodes.

### One walker, not ten parsers

Every profile section — experience, education, certifications, projects — comes
back as the *same* recursive component tree, because LinkedIn renders them all
with one generic UI:

```jsonc
entityComponent: {
  titleV2:  { text: { text: "Senior Engineer" } },
  subtitle: { text: "Acme Corp · Full-time" },
  caption:  { text: "Mar 2021 - Present · 3 yrs 2 mos" },
  metadata: { text: "London, United Kingdom · Hybrid" },
  subComponents: { components: [ …description, nested roles… ] }
}
```

So `app/parsing/components.py` implements **one** walker that flattens any
component into `(title, subtitle, caption, metadata, description, image, link,
children)`. Each section module is then a thin mapper deciding what those slots
*mean* — for experience the subtitle is the company; for education it is the
degree. Adding a section is a ~40-line mapper, not a new parser.

Details that matter in practice:

- **Multi-role employers.** Several roles at one company arrive as the *company*
  entity with roles nested as children. Flattening naively invents a job titled
  "Acme Corp" and discards the promotion history, so children are expanded into
  separate entries that inherit the employer.
- **Typographic dashes.** LinkedIn renders ranges as `2019 – 2023` with an en
  dash. Matching only `-` silently drops every date.
- **Images.** LinkedIn never returns a usable image URL, only a `rootUrl` plus
  per-width path segments that must be recombined. Every size is kept.
- **Degrees with commas.** `"BSc, Honours, Physics"` splits on the *last* comma,
  since degree names contain commas but fields of study rarely do.

### Findings from probing live LinkedIn

Three defects were found by testing against production LinkedIn rather than
assumptions, each now covered by regression tests:

- **Trailing-slash redirect.** LinkedIn 301s `/in/<slug>/` → `/in/<slug>`.
  Treating redirects as errors broke the HTML tier even with a valid session.
- **Routing cookies must be echoed.** LinkedIn issues `lidc` (datacenter
  pinning) plus `bcookie`/`bscookie` and expects them back. A client that
  rebuilds the Cookie header from only `li_at` never sends them, and LinkedIn
  answers with a 302 to the same URL trying to set them — an apparent infinite
  redirect. The session now accumulates what LinkedIn sets.
- **Request fingerprinting.** Page loads were being sent with XHR headers
  (`origin`, `csrf-token`, `x-li-track`, `sec-fetch-mode: cors`). Chrome never
  does that on a document navigation. Page requests now use
  `sec-fetch-mode: navigate` / `sec-fetch-dest: document` and omit every
  XHR-only header.

---

## Architecture

### Three acquisition tiers

Tried in order; whichever answers is reported in `meta.source`.

| Tier | Source | Depends on | Notes |
|---|---|---|---|
| 1 | `voyager_graphql` | live `queryId` | Primary. Richest data. |
| 2 | `voyager_rest` | legacy `profileView` | **Structured** dates, so highest fidelity where it survives. Being retired by LinkedIn. |
| 3 | `embedded_html` | nothing | Safety net. |

**Tier 3 is the interesting one.** LinkedIn server-renders pages with a BigPipe
pattern that inlines the API responses the page is about to need:

```html
<code style="display:none" id="bpr-guid-1234567">{"data":…,"included":[…]}</code>
```

Those are byte-for-byte the same Voyager payloads the GraphQL endpoints return,
so **the existing parsers consume them unchanged**. It needs no `queryId`, so it
survives the rotation that kills Tier 1 — and it is still a plain authenticated
`GET`, no browser and no JS execution. It ranks third only because LinkedIn
inlines just what the initial viewport needs, so long sections arrive truncated
(disclosed via `warnings`).

The tiers fail independently, which is the entire point: the failure mode that
kills one usually leaves the others standing.

### Degradation, not failure

A failed section becomes `[]` plus a `warnings[]` entry and a `completeness`
flag — never a 502. A missing certifications section must not sink an otherwise
good profile.

Some failures deliberately short-circuit rather than trying all three tiers: a
404, a private profile, an invalid cookie, or no session at all would produce the
same answer from every tier, so retrying them just adds load and slower errors.

### Protecting the account

The service holds live credentials for a real LinkedIn account, so most of the
reliability work protects *that*, not the API:

- **Session pool** — least-recently-used rotation, per-session request budget,
  automatic cooldown, and quarantine on rejection.
- **Never retry auth failures.** Retrying a `401` cannot help and only
  accelerates a restriction; the session is cooled down instead.
- **Paced requests** with randomised jitter — uniform spacing is itself a
  fingerprint.
- **Caching**, because every miss is upstream load. Redis when `REDIS_URL` is
  set, in-process TTL+LRU otherwise.
- **Request coalescing** — concurrent requests for the same profile share one
  upstream fetch instead of triggering N section fan-outs.
- **API-key auth on our own endpoint**, since an open endpoint backed by real
  credentials is an open proxy onto someone's LinkedIn account.

### Layout

```
app/
├── main.py                 app factory, lifespan, middleware
├── config.py               settings; all secrets from env
├── api/                    routes, dependencies, error handlers
├── linkedin/
│   ├── client.py           HTTP transport: headers, retries, redirects, pacing
│   ├── session.py          one session + cookie jar + health
│   ├── session_pool.py     rotation and quarantine
│   ├── query_ids.py        3-tier queryId resolution + bundle scraping
│   ├── endpoints.py        Rest.li URL builders
│   ├── html_fallback.py    bpr-guid embedded-JSON extraction (tier 3)
│   └── fetcher.py          tier orchestration and degradation
├── parsing/
│   ├── collection.py       URN index for the normalised format
│   ├── components.py       the generic component walker
│   ├── images.py dates.py urn.py
│   └── sections/           thin per-section mappers
├── models/                 Pydantic response schema + flat projection
├── services/               cache, profile service
└── observability/          structlog (with credential redaction), metrics
```

---

## Deployment

### Render (blueprint included)

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → select the repo. It reads `render.yaml`.
3. Set the secrets when prompted — they are marked `sync: false` so they are
   **never** stored in the repo:
   - `LINKEDIN_LI_AT`
   - `API_KEYS`
4. Deploy. HTTPS and a subdomain are automatic.

```bash
curl -H "X-API-Key: $KEY" \
  "https://<your-service>.onrender.com/api/v1/profile?url=williamhgates"
```

> Render's free tier sleeps after inactivity, so the first request after a
> pause takes ~50s. The generous cache TTL in `render.yaml` compensates.

### Any container host

```bash
docker build -t linkedin-profile-api .
docker run -p 8000:8000 -e LINKEDIN_LI_AT=… -e API_KEYS=… linkedin-profile-api
```

The image runs as an unprivileged user, carries no build toolchain, and honours
`$PORT` — so it works as-is on Fly.io, Railway, Cloud Run and similar.

### Configuration

Full list in [`.env.example`](.env.example). The ones that matter:

| Variable | Default | Purpose |
|---|---|---|
| `LINKEDIN_LI_AT` | — | **Required.** Session cookie. |
| `API_KEYS` | — | Comma-separated keys for `X-API-Key`. |
| `REQUIRE_API_KEY` | `true` | Set `false` for local development. |
| `REDIS_URL` | — | Shared cache; falls back to in-process. |
| `CACHE_TTL_SECONDS` | `3600` | Higher means less upstream load. |
| `REQUESTS_PER_MINUTE_PER_SESSION` | `30` | Per-session budget. |
| `MIN_DELAY_BETWEEN_REQUESTS_MS` | `350` | Pacing floor. |
| `ENABLE_GRAPHQL_TIER` / `_REST_` / `_HTML_` | `true` | Toggle tiers to test the fallback chain. |

---

## Development

```bash
pytest                       # full suite, entirely offline
pytest --cov=app             # with coverage
ruff check app/ tests/       # lint
mypy app/                    # type check
```

**The suite never touches LinkedIn.** Upstream is mocked at the HTTP transport
layer with `respx`, so CI needs no cookie and sends no traffic. Parsers are
tested against fixtures that model the real payload shapes.

```
142 tests · ruff clean · mypy clean
```

Tests are organised by what they protect:

| File | Covers |
|---|---|
| `unit/test_components.py` | the generic walker |
| `unit/test_sections.py` | section mappers, incl. multi-role employers |
| `unit/test_primitives.py` | URLs, URNs, dates, images, URN index |
| `unit/test_infrastructure.py` | cache, session pool, HTML extraction, log redaction |
| `integration/test_client.py` | headers, status mapping, retries, session health |
| `integration/test_fetcher_tiers.py` | the three-tier fallback chain |
| `integration/test_query_ids.py` | discovery, overrides, re-discovery |
| `integration/test_redirects.py` | redirect handling, expired-cookie detection |
| `integration/test_headers.py` | XHR vs navigation fingerprints |
| `integration/test_cookie_jar.py` | cookie persistence |
| `integration/test_api.py` | auth, validation, errors, caching, both formats |

### Reconnaissance

`scripts/recon.py` probes live LinkedIn with your cookie and reports which tiers
your account can reach, discovers today's `queryId` hashes, and captures real
responses as fixtures. `scripts/sanitize_fixture.py` scrubs PII from captures
before they are committed.

```bash
python scripts/recon.py williamhgates            # capture sanitised fixtures
python scripts/recon.py williamhgates --no-save  # probe only
```

### Secrets

`.env` is gitignored and CI fails the build if it ever becomes tracked, with
gitleaks scanning on top. Session cookies are stripped from every log line by a
structlog processor, and `/readyz` reports session health without ever exposing
cookie values. Both are covered by tests.

---

## Known limitations

**LinkedIn actively defends against this.** The honest framing: this works, and
it is not bulletproof.

1. **Cookies expire and get revoked.** Signing out of the browser invalidates the
   copied `li_at` immediately. LinkedIn also revokes sessions it considers
   suspicious — including on repeated CSRF failures, which is why supplying a
   mismatched `JSESSIONID` is worse than supplying none.

2. **`queryId` rotation.** Mitigated by runtime discovery and two independent
   fallback tiers, but a LinkedIn change that breaks discovery *and* both
   fallbacks would need a code change.

3. **Datacenter IPs are treated with more suspicion** than residential ones. A
   cloud deployment is likelier to be challenged than a local run. Fixed by
   neither caching nor pacing — it is a property of where you deploy.

4. **Rate limits are real and unpublished.** Defaults are conservative
   (30 req/min/session, ~350 ms spacing). Raising them is the fastest way to get
   an account restricted. Scale by adding accounts, not by loosening pacing.

5. **Visibility limits what you can see.** Out-of-network profiles return less;
   contact details appear only where the member shared them; connection counts
   cap at "500+". Absent data is reported as `null`, never invented.

6. **The legacy REST tier is being retired.** It returns *structured* dates and
   is the highest-fidelity source where it works, but LinkedIn has already
   disabled it for many accounts.

7. **Tier 3 truncates.** Embedded page data covers the initial viewport, so long
   experience lists may be cut short. Always disclosed via `warnings` and
   `meta.completeness`.

8. **Rate limiting is per-instance.** The sliding window is in-process, so
   horizontal scaling multiplies the effective limit. Redis-backed limiting would
   be the fix if that mattered.

9. **No pagination for very long sections.** Sections beyond the first page are
   fetched where the endpoint supports it, but profiles with dozens of entries
   may still be truncated.

10. **English only.** Locale is pinned to `en_US`; the caption parsers assume
    English month names and duration strings.

---

## Legal and responsible use

Automated collection of LinkedIn data **violates LinkedIn's User Agreement**,
regardless of how it is implemented. This repository exists as a technical
demonstration for a hiring challenge.

Before using it, consider:

- **Terms of Service.** LinkedIn prohibits automated access. Accounts used this
  way can be restricted or permanently banned. Use a throwaway account.
- **Data protection law.** LinkedIn profiles contain personal data. Under the
  GDPR, the UK GDPR, the CCPA and similar regimes, collecting and storing it
  makes you a data controller with real obligations — lawful basis, retention
  limits, and subject access rights among them. "It was publicly visible" is not
  by itself a lawful basis.
- **The people in the data.** Profiles describe real individuals who did not
  consent to being scraped.

Do not use this for bulk harvesting, unsolicited outreach, or building
datasets about people without a lawful basis. The maintainers accept no
liability for how it is used.

## License

MIT. See [LICENSE](LICENSE).
