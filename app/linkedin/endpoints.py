"""URL builders for the Voyager endpoints this service uses.

LinkedIn's GraphQL gateway does not accept a JSON body of variables the way a
public GraphQL API would. It uses **Rest.li 2.0 parameter encoding**, where
structured arguments are serialised inline into the query string:

    ?variables=(profileUrn:urn%3Ali%3Afsd_profile%3AACoAAA...,sectionType:experience)
    &queryId=voyagerIdentityDashProfileCards.<hash>

Note the shape: parentheses delimit an object, ``key:value`` pairs are
comma-separated, ``List(...)`` wraps arrays, and only the *values* are
percent-encoded — encoding the parentheses or colons breaks the parse. Getting
this encoding subtly wrong is the second most common reason a reimplementation
gets a 400 back (the first being a rotated queryId).
"""

from __future__ import annotations

from urllib.parse import quote

BASE_URL = "https://www.linkedin.com"
VOYAGER_BASE = f"{BASE_URL}/voyager/api"
GRAPHQL_URL = f"{VOYAGER_BASE}/graphql"


def restli_encode(value: str) -> str:
    """Percent-encode a Rest.li *value*, leaving the structural syntax alone.

    Colons inside URNs must be escaped (``%3A``) or Rest.li reads them as
    key/value separators.
    """
    return quote(str(value), safe="")


def restli_object(pairs: dict[str, str]) -> str:
    """Serialise ``{"a": "b"}`` to Rest.li's ``(a:b)`` object form."""
    inner = ",".join(f"{k}:{v}" for k, v in pairs.items())
    return f"({inner})"


def restli_list(values: list[str]) -> str:
    """Serialise a list to Rest.li's ``List(a,b,c)`` form."""
    return f"List({','.join(values)})"


# --------------------------------------------------------------- tier 1: graphql


def profile_by_vanity_url(vanity_name: str, query_id: str) -> str:
    """Resolve a public identifier to the profile's ``fsd_profile`` URN."""
    variables = restli_object({"vanityName": restli_encode(vanity_name)})
    return f"{GRAPHQL_URL}?variables={variables}&queryId={query_id}"


def profile_cards_url(profile_urn: str, section: str, query_id: str, locale: str = "en_US") -> str:
    """Fetch one profile section (experience, education, skills, ...)."""
    variables = restli_object(
        {
            "profileUrn": restli_encode(profile_urn),
            "sectionType": section,
            "locale": locale,
        }
    )
    return f"{GRAPHQL_URL}?variables={variables}&queryId={query_id}"


def profile_components_url(
    profile_urn: str,
    section: str,
    query_id: str,
    *,
    count: int = 50,
    start: int = 0,
    locale: str = "en_US",
) -> str:
    """Paginated fetch for sections whose card view is truncated.

    A profile with 30 roles only shows a handful on the card; the rest come
    from this endpoint, which is why long careers look truncated in naive
    implementations.
    """
    variables = restli_object(
        {
            "profileUrn": restli_encode(profile_urn),
            "sectionType": section,
            "locale": locale,
            "count": str(count),
            "start": str(start),
        }
    )
    return f"{GRAPHQL_URL}?variables={variables}&queryId={query_id}"


# ------------------------------------------------- tier 1: dash (current, verified)

#: Rest.li projection selecting the profile *and* its related entities —
#: positions, educations, companies, schools — in a single response. Verified
#: working against live LinkedIn; the number is the decoration version and is
#: the part most likely to need bumping if LinkedIn revises the schema.
DECO_FULL_PROFILE = "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-101"
DECO_TOP_CARD = "com.linkedin.voyager.dash.deco.identity.profile.TopCardCore-6"


def dash_profile_url(public_id: str, *, decoration: str = DECO_FULL_PROFILE) -> str:
    """Look a profile up by public identifier on the modern dash endpoint.

    This is the working replacement for the retired ``profileView``. It is a
    Rest.li *finder* (``q=memberIdentity``) rather than a GraphQL persisted
    query, which is why it needs **no queryId** — and therefore cannot be
    broken by LinkedIn rotating those hashes.
    """
    return (
        f"{VOYAGER_BASE}/identity/dash/profiles"
        f"?q=memberIdentity&memberIdentity={restli_encode(public_id)}"
        f"&decorationId={decoration}"
    )


# ------------------------------------------------------------ tier 3: rest (legacy)


def profile_view_url(public_id: str) -> str:
    """The legacy aggregate endpoint.

    Observed returning **410 Gone** against live LinkedIn — it has been
    retired. Kept because it still answers for some older accounts and costs
    nothing to try once the tiers above have failed.
    """
    return f"{VOYAGER_BASE}/identity/profiles/{restli_encode(public_id)}/profileView"


def profile_contact_info_url(public_id: str) -> str:
    return f"{VOYAGER_BASE}/identity/profiles/{restli_encode(public_id)}/profileContactInfo"


def profile_skills_url(public_id: str, *, count: int = 100, start: int = 0) -> str:
    return (
        f"{VOYAGER_BASE}/identity/profiles/{restli_encode(public_id)}/skills"
        f"?count={count}&start={start}"
    )


def profile_network_info_url(public_id: str) -> str:
    """Connection and follower counts, which profileView omits."""
    return (
        f"{VOYAGER_BASE}/identity/profiles/{restli_encode(public_id)}"
        "/networkinfo"
    )


# ------------------------------------------------------------- tier 3: html


def profile_html_url(public_id: str) -> str:
    """The canonical profile page URL.

    Deliberately **no trailing slash**: LinkedIn 301-redirects ``/in/<slug>/``
    to ``/in/<slug>``, so including one costs an extra round trip on every
    HTML-tier fetch. Verified against live LinkedIn.
    """
    return f"{BASE_URL}/in/{quote(public_id, safe='')}"


def me_url() -> str:
    """Identity of the authenticated account — the cheapest credential check."""
    return f"{VOYAGER_BASE}/me"


#: Section identifiers accepted by the profile-cards endpoint.
PROFILE_SECTIONS: tuple[str, ...] = (
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
    "patents",
    "organizations",
    "test_scores",
    "interests",
)
