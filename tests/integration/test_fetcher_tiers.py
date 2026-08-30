"""The three-tier fallback chain.

This is the core resilience claim of the design, so it is tested by actually
breaking each tier and asserting the next one carries the request — not by
inspecting that the code contains a try/except.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.linkedin.exceptions import AllTiersFailedError, AuthenticationError, ProfileNotFoundError
from app.linkedin.fetcher import ProfileFetcher
from app.models.profile import Source
from tests.factories import card, entity_component

DASH = "https://www.linkedin.com/voyager/api/identity/dash/profiles"
GRAPHQL = "https://www.linkedin.com/voyager/api/graphql"
PROFILE_VIEW = "https://www.linkedin.com/voyager/api/identity/profiles/testuser/profileView"
CONTACT_INFO = (
    "https://www.linkedin.com/voyager/api/identity/profiles/testuser/profileContactInfo"
)
HTML_URL = "https://www.linkedin.com/in/testuser"

PROFILE_URN = "urn:li:fsd_profile:ACoAAATEST"


def _urn_lookup_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {},
            "included": [
                {
                    "entityUrn": PROFILE_URN,
                    "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                    "firstName": "Ada",
                    "lastName": "Lovelace",
                    "headline": "Mathematician",
                }
            ],
        },
    )


def _rest_profile_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {},
            "included": [
                {
                    "entityUrn": PROFILE_URN,
                    "$type": "com.linkedin.voyager.identity.profile.Profile",
                    "firstName": "Ada",
                    "lastName": "Lovelace",
                    "headline": "Mathematician",
                    "summary": "Analytical engines.",
                    "locationName": "London, United Kingdom",
                },
                {
                    "entityUrn": "urn:li:fs_position:1",
                    "$type": "com.linkedin.voyager.identity.profile.Position",
                    "title": "Analyst",
                    "companyName": "Analytical Engine Co",
                    "timePeriod": {"startDate": {"month": 1, "year": 1843}},
                },
            ],
        },
    )


def _html_page() -> str:
    payload = (
        '{"data":{},"included":[{"entityUrn":"' + PROFILE_URN + '",'
        '"$type":"com.linkedin.voyager.identity.profile.Profile",'
        '"firstName":"Ada","lastName":"Lovelace","headline":"From HTML"}]}'
    )
    return (
        "<html><body><div id='main'>profile</div>"
        f'<code style="display:none" id="bpr-guid-1234567">{payload}</code>'
        "</body></html>"
    )


def _fetcher(client, resolver, **kwargs) -> ProfileFetcher:
    """Fetcher with the dash tier off by default.

    These tests predate the dash tier and exercise the graphql -> rest -> html
    chain specifically; dash has its own tests in test_dash_tier.py.
    """
    kwargs.setdefault("enable_dash", False)
    return ProfileFetcher(client, resolver, **kwargs)


# ------------------------------------------------------------------- tier 1


@respx.mock
async def test_graphql_tier_assembles_a_profile(client, resolver) -> None:
    experience = card(
        "experience",
        [
            entity_component(
                title="Senior Engineer",
                subtitle="Acme Corp · Full-time",
                caption="Mar 2021 - Present · 3 yrs",
            )
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        variables = str(request.url)
        if "vanityName" in variables:
            return _urn_lookup_response()
        if "sectionType:experience" in variables:
            return httpx.Response(200, json=experience)
        return httpx.Response(200, json={"data": {}, "included": []})

    respx.get(url__startswith=GRAPHQL).mock(side_effect=handler)

    result = await _fetcher(client, resolver).fetch("testuser")

    assert result.source is Source.GRAPHQL
    assert result.profile_urn == PROFILE_URN
    assert result.profile.basics.full_name == "Ada Lovelace"
    assert [e.title for e in result.profile.experience] == ["Senior Engineer"]
    assert result.completeness["experience"] is True
    assert result.completeness["education"] is False


@respx.mock
async def test_one_failing_section_does_not_fail_the_profile(client, resolver) -> None:
    """A partial profile beats an error."""
    experience = card("experience", [entity_component(title="Engineer", subtitle="Acme")])

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "vanityName" in url:
            return _urn_lookup_response()
        if "sectionType:experience" in url:
            return httpx.Response(200, json=experience)
        if "sectionType:certifications" in url:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"data": {}, "included": []})

    respx.get(url__startswith=GRAPHQL).mock(side_effect=handler)

    result = await _fetcher(client, resolver).fetch("testuser")

    assert result.source is Source.GRAPHQL
    assert result.profile.experience, "the good section must still be returned"
    assert result.profile.certifications == []
    assert any("certifications" in w for w in result.warnings), result.warnings
    assert result.completeness["certifications"] is False


# --------------------------------------------------------- tier 1 -> tier 2


@respx.mock
async def test_falls_back_to_rest_when_graphql_query_ids_are_rotated(client, resolver) -> None:
    """The rotation scenario this whole design exists to survive."""
    respx.get(url__startswith=GRAPHQL).mock(
        return_value=httpx.Response(400, text='{"message":"unknown queryId"}')
    )
    respx.get(url__startswith=PROFILE_VIEW).mock(return_value=_rest_profile_response())
    respx.get(url__startswith=CONTACT_INFO).mock(
        return_value=httpx.Response(200, json={"data": {}, "included": []})
    )

    result = await _fetcher(client, resolver).fetch("testuser")

    assert result.source is Source.REST
    assert result.profile.basics.full_name == "Ada Lovelace"
    assert result.profile.basics.about == "Analytical engines."
    assert result.profile.experience[0].company == "Analytical Engine Co"
    # The REST tier carries structured dates, so they are exact.
    assert result.profile.experience[0].dates.start.year == 1843  # type: ignore[union-attr]


@respx.mock
async def test_graphql_is_skipped_entirely_when_no_query_ids_are_known(
    client, resolver
) -> None:
    """With no ids and discovery off, tier 1 must step aside, not send a bad request."""
    resolver._ids = {}
    graphql = respx.get(url__startswith=GRAPHQL).mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(url__startswith=PROFILE_VIEW).mock(return_value=_rest_profile_response())
    respx.get(url__startswith=CONTACT_INFO).mock(
        return_value=httpx.Response(200, json={"data": {}, "included": []})
    )

    result = await _fetcher(client, resolver).fetch("testuser")

    assert result.source is Source.REST
    assert graphql.call_count == 0, "must not send a request guaranteed to 400"


# --------------------------------------------------------- tier 2 -> tier 3


@respx.mock
async def test_falls_all_the_way_back_to_embedded_html(client, resolver) -> None:
    """Tier 3 needs no queryId, so it survives what kills tiers 1 and 2."""
    respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(400, text="queryId"))
    respx.get(url__startswith=PROFILE_VIEW).mock(return_value=httpx.Response(500, text="gone"))
    respx.get(url__startswith=HTML_URL).mock(
        return_value=httpx.Response(200, text=_html_page())
    )

    result = await _fetcher(client, resolver).fetch("testuser")

    assert result.source is Source.HTML
    assert result.profile.basics.headline == "From HTML"
    assert any("truncated" in w for w in result.warnings), "truncation must be disclosed"


@respx.mock
async def test_all_tiers_failing_raises_a_combined_error(client, resolver) -> None:
    respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=PROFILE_VIEW).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=HTML_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(AllTiersFailedError):
        await _fetcher(client, resolver).fetch("testuser")


# ------------------------------------------------------------ short circuits


@respx.mock
async def test_a_404_stops_immediately_without_trying_other_tiers(client, resolver) -> None:
    """No tier can find a profile that does not exist; don't hammer LinkedIn twice more."""
    respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(404))
    rest = respx.get(url__startswith=PROFILE_VIEW).mock(return_value=httpx.Response(200))
    html = respx.get(url__startswith=HTML_URL).mock(return_value=httpx.Response(200))

    with pytest.raises(ProfileNotFoundError):
        await _fetcher(client, resolver).fetch("nosuchuser")

    assert rest.call_count == 0
    assert html.call_count == 0


@respx.mock
async def test_a_bad_cookie_stops_immediately(client, resolver) -> None:
    """Every tier shares one credential, so retrying them all is pointless."""
    respx.get(url__startswith=GRAPHQL).mock(return_value=httpx.Response(401))
    rest = respx.get(url__startswith=PROFILE_VIEW).mock(return_value=httpx.Response(200))

    with pytest.raises(AuthenticationError):
        await _fetcher(client, resolver).fetch("testuser")

    assert rest.call_count == 0
