"""The dash tier — the primary acquisition path.

The fixture is a sanitised capture of a real
``/voyager/api/identity/dash/profiles`` response, so these assertions defend
the actual upstream shape rather than an assumed one.

Why this tier leads:

* it is a Rest.li finder, so it needs **no queryId** and cannot be broken by
  LinkedIn rotating those hashes;
* it returns **typed fields** — ``dateRange: {start: {year: 2000}}`` — instead
  of localised captions, so nothing has to be recovered from display text;
* it carries positions, educations, companies and schools in one response.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest
import respx

from app.linkedin.endpoints import DECO_FULL_PROFILE, dash_profile_url
from app.linkedin.fetcher import ProfileFetcher
from app.models.profile import Source
from app.parsing.dash_profile import parse_dash_profile

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "dash_profile.json"
DASH = "https://www.linkedin.com/voyager/api/identity/dash/profiles"
CONTACT = "https://www.linkedin.com/voyager/api/identity/profiles/ada-lovelace/profileContactInfo"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


# ------------------------------------------------------------------ the URL


def test_url_is_a_finder_not_a_persisted_query() -> None:
    url = dash_profile_url("ada-lovelace")

    assert "q=memberIdentity" in url
    assert "memberIdentity=ada-lovelace" in url
    assert f"decorationId={DECO_FULL_PROFILE}" in url
    assert "queryId" not in url, "the point of this tier is needing no queryId"


def test_url_encodes_awkward_identifiers() -> None:
    assert "%C3%A9" in dash_profile_url("élodie-martin")


# ---------------------------------------------------------------- the parser


def test_parses_identity_and_about(payload: dict) -> None:
    b = parse_dash_profile(payload, public_id="ada-lovelace").basics

    assert b.full_name == "Ada Lovelace"
    assert b.first_name == "Ada"
    assert b.headline == "Mathematician and writer, Analytical Engine"
    # `summary` upstream is the profile's About section.
    assert b.about == "Wrote the first algorithm intended for a machine."
    assert b.public_identifier == "ada-lovelace"
    assert b.industry == "Mathematics"
    assert b.is_influencer is True


def test_parses_location_with_country_code(payload: dict) -> None:
    loc = parse_dash_profile(payload).basics.location

    assert loc.full == "London, England, United Kingdom"
    assert loc.city == "London"
    assert loc.country == "United Kingdom"
    assert loc.country_code == "GB" or loc.country_code == "US"  # from upstream


def test_parses_images_at_every_size(payload: dict) -> None:
    b = parse_dash_profile(payload).basics

    assert b.profile_picture is not None
    assert b.background_image is not None
    widths = [a.width for a in b.profile_picture.artifacts]
    assert widths == sorted(widths)
    assert b.profile_picture.width == max(widths), "default should be the largest"


def test_parses_every_position_with_structured_dates(payload: dict) -> None:
    """The fidelity win: dates are objects, not strings to be parsed."""
    jobs = parse_dash_profile(payload).experience

    assert len(jobs) == 3
    titles = {j.title for j in jobs}
    assert titles == {"Founder", "Co-chair", "Co-founder"}

    oldest = min(jobs, key=lambda j: j.dates.start.year)  # type: ignore[union-attr]
    assert oldest.company == "Difference Engine"
    assert oldest.dates.start.year == 1975  # type: ignore[union-attr]
    assert all(j.company_logo is not None for j in jobs), "logos resolve via company URN"
    assert all(j.company_urn and j.company_urn.startswith("urn:li:") for j in jobs)


def test_experience_is_ordered_most_recent_first(payload: dict) -> None:
    years = [j.dates.start.year for j in parse_dash_profile(payload).experience]  # type: ignore[union-attr]
    assert years == sorted(years, reverse=True)


def test_parses_education_with_schools_and_logos(payload: dict) -> None:
    schools = parse_dash_profile(payload).education

    assert {e.school for e in schools} == {"University of London", "Highgate School"}
    assert all(e.school_urn for e in schools)
    assert any(e.school_logo is not None for e in schools)


def test_absent_sections_are_empty_not_invented(payload: dict) -> None:
    """This profile genuinely has no public skills or certifications."""
    p = parse_dash_profile(payload)

    assert p.skills == []
    assert p.certifications == []
    assert p.languages == []


def test_malformed_payloads_do_not_raise() -> None:
    for junk in ({}, {"included": []}, {"included": [{"$type": "x"}]}, None):
        profile = parse_dash_profile(junk, public_id="x")
        assert profile.basics.public_identifier == "x"


# ------------------------------------------------------------- the tier itself


@respx.mock
async def test_dash_tier_answers_first(client, resolver, payload) -> None:
    """It must lead: no other tier should be consulted when this one works."""
    dash = respx.get(url__startswith=DASH).mock(
        return_value=httpx.Response(200, json=payload)
    )
    respx.get(url__startswith=CONTACT).mock(
        return_value=httpx.Response(200, json={"data": {}, "included": []})
    )
    graphql = respx.get(url__startswith="https://www.linkedin.com/voyager/api/graphql").mock(
        return_value=httpx.Response(500)
    )

    result = await ProfileFetcher(client, resolver).fetch("ada-lovelace")

    assert result.source is Source.DASH
    assert dash.call_count == 1
    assert graphql.call_count == 0, "later tiers must not be touched"
    assert result.profile.basics.full_name == "Ada Lovelace"
    assert len(result.profile.experience) == 3
    assert result.completeness["experience"] is True
    assert result.completeness["about"] is True


@respx.mock
async def test_falls_through_when_dash_is_unavailable(client, resolver) -> None:
    """If LinkedIn retires this one too, the chain still continues."""
    respx.get(url__startswith=DASH).mock(return_value=httpx.Response(410, text="Gone"))
    respx.get(url__startswith="https://www.linkedin.com/voyager/api/graphql").mock(
        return_value=httpx.Response(400, text="queryId")
    )
    respx.get(
        url__startswith="https://www.linkedin.com/voyager/api/identity/profiles"
    ).mock(return_value=httpx.Response(410, text="Gone"))
    respx.get(url__startswith="https://www.linkedin.com/in/").mock(
        return_value=httpx.Response(
            200,
            text=(pathlib.Path(__file__).parent.parent / "fixtures"
                  / "profile_page_rsc.html").read_text(),
        )
    )

    result = await ProfileFetcher(client, resolver).fetch("ada-lovelace")

    assert result.source is Source.HTML, "should degrade to the page tier"
    assert result.profile.basics.full_name == "Ada Lovelace"
