"""Parsing LinkedIn's current profile page (React Server Components).

The fixture is derived from a real captured page — same Flight payload shape,
same hashed class names, same lazy-load markers — with the subject swapped out.
It is the structure these assertions defend, since LinkedIn's class names are
build-hashed and cannot be selected on.
"""

from __future__ import annotations

import pathlib

import pytest

from app.parsing.rsc_profile import (
    extract_flight_payload,
    looks_authenticated,
    parse_rsc_profile,
    profile_urn_from_page,
    sections_are_lazy_loaded,
    visible_text,
)

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "profile_page_rsc.html"


@pytest.fixture
def page() -> str:
    return FIXTURE.read_text()


def test_recognises_an_authenticated_page(page: str) -> None:
    assert looks_authenticated(page) is True
    assert looks_authenticated("<html><body>Sign in to LinkedIn</body></html>") is False
    assert looks_authenticated("<html>authwall</html>") is False


def test_extracts_the_flight_payload(page: str) -> None:
    blob = extract_flight_payload(page)
    assert blob, "the RSC payload should be recoverable"
    assert "firstName" in blob


def test_parses_identity_from_a_real_page_shape(page: str) -> None:
    b = parse_rsc_profile(page, public_id="ada-lovelace").basics

    assert b.full_name == "Ada Lovelace"
    assert b.first_name == "Ada"
    assert b.last_name == "Lovelace"
    assert b.headline == "Mathematician and writer, Analytical Engine"
    assert b.public_identifier == "ada-lovelace"
    assert b.profile_url.endswith("/in/ada-lovelace")  # type: ignore[union-attr]


def test_parses_location_into_parts(page: str) -> None:
    loc = parse_rsc_profile(page).basics.location

    assert loc.full == "London, England, United Kingdom"
    assert loc.city == "London"
    assert loc.country == "United Kingdom"


def test_recovers_every_image_size(page: str) -> None:
    """LinkedIn emits one photo at several widths; all are kept."""
    b = parse_rsc_profile(page).basics

    assert b.profile_picture is not None
    sizes = [a.width for a in b.profile_picture.artifacts]
    assert sizes == sorted(sizes), "artifacts should be ascending by width"
    assert b.profile_picture.width == max(sizes), "default is the largest"
    assert "displayphoto" in b.profile_picture.url


def test_extracts_the_profile_urn(page: str) -> None:
    urn = profile_urn_from_page(page)
    assert urn is not None
    assert urn.startswith("urn:li:fsd_profile:")
    # LinkedIn sometimes double-prefixes; the result must be normalised.
    assert urn.count("urn:li:fsd_profile:") == 1


def test_detects_that_sections_are_lazy_loaded(page: str) -> None:
    """Their absence is a property of the page, not a parsing failure."""
    assert sections_are_lazy_loaded(page) is True

    profile = parse_rsc_profile(page)
    assert profile.experience == []
    assert profile.education == []


def test_external_websites_only(page: str) -> None:
    sites = parse_rsc_profile(page).basics.contact.websites
    assert all("linkedin.com" not in s and "licdn.com" not in s for s in sites)


def test_malformed_pages_do_not_raise() -> None:
    for junk in ("", "<html></html>", "<script id='rehydrate-data'>not json</script>"):
        profile = parse_rsc_profile(junk, public_id="x")
        assert profile.basics.public_identifier == "x"


def test_visible_text_drops_scripts_and_styles() -> None:
    text = visible_text(
        "<html><style>p{color:red}</style><script>var x=1</script>"
        "<p>Real content</p></html>"
    )
    joined = " ".join(text)
    assert "Real content" in joined
    assert "color:red" not in joined
    assert "var x" not in joined
