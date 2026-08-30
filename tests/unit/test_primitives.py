"""URL normalisation, URNs, dates, images, and the normalised-response index."""

from __future__ import annotations

import pytest

from app.linkedin.exceptions import InvalidProfileUrlError
from app.linkedin.profile_url import extract_public_identifier
from app.parsing.collection import EntityIndex, dig, text_of
from app.parsing.dates import parse_caption_range, parse_structured_range
from app.parsing.images import parse_vector_image
from app.parsing.urn import company_url_from_urn, normalize_profile_urn, parse_urn

# ------------------------------------------------------------------ profile URL


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/in/williamhgates",
        "https://www.linkedin.com/in/williamhgates/",
        "http://linkedin.com/in/williamhgates",
        "www.linkedin.com/in/williamhgates",
        "linkedin.com/in/williamhgates",
        "https://uk.linkedin.com/in/williamhgates",
        "https://m.linkedin.com/in/williamhgates",
        "https://www.linkedin.com/in/williamhgates?originalSubdomain=uk",
        "https://www.linkedin.com/in/williamhgates/detail/recent-activity/",
        "williamhgates",
    ],
)
def test_all_url_shapes_collapse_to_one_identifier(raw: str) -> None:
    """Every shape must yield the same cache key, or the cache silently misses."""
    assert extract_public_identifier(raw) == "williamhgates"


def test_percent_encoded_unicode_slug_is_decoded() -> None:
    assert extract_public_identifier("https://www.linkedin.com/in/%C3%A9lodie-martin") == (
        "élodie-martin"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://example.com/in/someone",
        "https://www.linkedin.com/company/acme",
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/school/mit/",
        "https://twitter.com/someone",
    ],
)
def test_non_profile_inputs_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidProfileUrlError):
        extract_public_identifier(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil-linkedin.com/in/victim",
        "https://linkedin.com.attacker.net/in/victim",
        "https://notlinkedin.com/in/victim",
    ],
)
def test_lookalike_hosts_are_rejected(raw: str) -> None:
    """Host matching must be on a label boundary, not a bare suffix.

    `"evil-linkedin.com".endswith("linkedin.com")` is True, so a naive suffix
    check would send authenticated cookies to an attacker-controlled host.
    """
    with pytest.raises(InvalidProfileUrlError):
        extract_public_identifier(raw)


def test_rejection_message_names_the_problem() -> None:
    with pytest.raises(InvalidProfileUrlError, match="company"):
        extract_public_identifier("https://www.linkedin.com/company/acme")


# ------------------------------------------------------------------------ URNs


def test_urn_parsing_and_normalisation() -> None:
    assert parse_urn("urn:li:fsd_profile:ACoAAA") == ("fsd_profile", "ACoAAA")
    assert parse_urn("not-a-urn") is None
    # The legacy mini-profile namespace must coerce to the modern one.
    assert normalize_profile_urn("urn:li:fs_miniProfile:ACoAAA") == "urn:li:fsd_profile:ACoAAA"
    assert normalize_profile_urn("ACoAAA") == "urn:li:fsd_profile:ACoAAA"
    assert normalize_profile_urn(None) is None


def test_company_and_school_urls_from_urns() -> None:
    assert company_url_from_urn("urn:li:fsd_company:1441") == (
        "https://www.linkedin.com/company/1441/"
    )
    assert company_url_from_urn("urn:li:fsd_school:1234") == (
        "https://www.linkedin.com/school/1234/"
    )
    assert company_url_from_urn("urn:li:fsd_profile:ACoAAA") is None


# ----------------------------------------------------------------------- dates


def test_structured_range_from_the_rest_tier() -> None:
    dr = parse_structured_range({"startDate": {"month": 3, "year": 2021}, "endDate": None})
    assert dr is not None
    assert dr.start is not None and (dr.start.year, dr.start.month) == (2021, 3)
    assert dr.is_current is True


def test_caption_without_a_date_is_not_a_date() -> None:
    """A location caption must not be coerced into a bogus range."""
    assert parse_caption_range("London, United Kingdom") is None
    assert parse_caption_range("") is None
    assert parse_caption_range(None) is None


def test_invalid_month_is_discarded_not_clamped() -> None:
    dr = parse_structured_range({"startDate": {"month": 13, "year": 2021}})
    assert dr is not None and dr.start is not None
    assert dr.start.month is None, "an out-of-range month should be dropped, not kept"
    assert dr.start.year == 2021


# ---------------------------------------------------------------------- images


def test_vector_image_is_reassembled_at_every_size() -> None:
    img = parse_vector_image(
        {
            "rootUrl": "https://media.licdn.com/dms/image/ABC/",
            "artifacts": [
                {"width": 100, "height": 100, "fileIdentifyingUrlPathSegment": "100_100/x"},
                {"width": 800, "height": 800, "fileIdentifyingUrlPathSegment": "800_800/x"},
                {"width": 400, "height": 400, "fileIdentifyingUrlPathSegment": "400_400/x"},
            ],
        }
    )
    assert img is not None
    assert img.url == "https://media.licdn.com/dms/image/ABC/800_800/x", "largest wins"
    assert [a.width for a in img.artifacts] == [100, 400, 800], "sorted ascending"


def test_wrapped_vector_images_are_unwrapped() -> None:
    inner = {
        "rootUrl": "https://media.licdn.com/x/",
        "artifacts": [{"width": 200, "height": 200, "fileIdentifyingUrlPathSegment": "200/y"}],
    }
    for wrapper in (
        {"vectorImage": inner},
        {"com.linkedin.common.VectorImage": inner},
        {"attributes": [{"detailData": {"vectorImage": inner}}]},
    ):
        img = parse_vector_image(wrapper)
        assert img is not None, f"failed to unwrap {next(iter(wrapper))}"
        assert img.url == "https://media.licdn.com/x/200/y"


def test_image_without_artifacts_is_none() -> None:
    assert parse_vector_image({"rootUrl": "https://x/", "artifacts": []}) is None
    assert parse_vector_image(None) is None
    assert parse_vector_image({"nope": 1}) is None


# ------------------------------------------------------- normalised responses


def test_entity_index_dereferences_star_prefixed_keys() -> None:
    payload = {
        "data": {"*profile": "urn:li:fsd_profile:ABC"},
        "included": [
            {
                "entityUrn": "urn:li:fsd_profile:ABC",
                "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                "firstName": "Ada",
            }
        ],
    }
    index = EntityIndex(payload)

    assert len(index) == 1
    assert index.get("urn:li:fsd_profile:ABC")["firstName"] == "Ada"  # type: ignore[index]

    resolved = index.resolve(index.data)
    assert resolved["profile"]["firstName"] == "Ada", "the * prefix should be stripped"
    assert resolved["profileUrn"] == "urn:li:fsd_profile:ABC", "raw URN kept alongside"


def test_entity_index_survives_reference_cycles() -> None:
    """A profile references a position that references the profile back."""
    payload = {
        "data": {"*root": "urn:li:a"},
        "included": [
            {"entityUrn": "urn:li:a", "$type": "A", "*peer": "urn:li:b"},
            {"entityUrn": "urn:li:b", "$type": "B", "*peer": "urn:li:a"},
        ],
    }
    resolved = EntityIndex(payload).resolve({"*root": "urn:li:a"})
    # Terminates, and the cycle is cut by handing back the bare URN.
    assert resolved["root"]["peer"]["peer"] == "urn:li:a"


def test_by_type_matches_across_namespaces() -> None:
    """`voyager.identity` and `voyager.dash.identity` coexist in one response."""
    payload = {
        "included": [
            {"entityUrn": "urn:li:1", "$type": "com.linkedin.voyager.identity.profile.Position"},
            {
                "entityUrn": "urn:li:2",
                "$type": "com.linkedin.voyager.dash.identity.profile.Position",
            },
        ]
    }
    assert len(EntityIndex(payload).by_type("Position")) == 2


def test_dig_and_text_of_tolerate_missing_paths() -> None:
    assert dig({"a": {"b": {"c": 1}}}, "a", "b", "c") == 1
    assert dig({"a": None}, "a", "b", default="fallback") == "fallback"
    assert dig(None, "a") is None
    # Voyager wraps single values in one-element lists.
    assert dig({"a": [{"b": 2}]}, "a", "b") == 2

    assert text_of("hello") == "hello"
    assert text_of({"text": "hello"}) == "hello"
    assert text_of({"text": {"text": "nested"}}) == "nested"
    assert text_of({"nope": 1}) is None
    assert text_of("   ") is None
