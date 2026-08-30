"""The generic component walker — the piece every section mapper sits on."""

from __future__ import annotations

from app.parsing.components import (
    flatten_card_payload,
    flatten_entity,
    flatten_section,
    iter_components,
)
from tests.factories import card, entity_component, vector_image


def test_flattens_a_simple_entity() -> None:
    payload = card(
        "experience",
        [
            entity_component(
                title="Senior Software Engineer",
                subtitle="Acme Corp · Full-time",
                caption="Mar 2021 - Present · 3 yrs 2 mos",
                metadata="London, United Kingdom · Hybrid",
                descriptions=["Led the payments rewrite."],
            )
        ],
    )
    entities = flatten_card_payload(payload)

    assert len(entities) == 1
    e = entities[0]
    assert e.title == "Senior Software Engineer"
    assert e.subtitle == "Acme Corp · Full-time"
    assert e.caption == "Mar 2021 - Present · 3 yrs 2 mos"
    assert e.metadata == "London, United Kingdom · Hybrid"
    assert e.description == "Led the payments rewrite."


def test_walks_through_nested_containers() -> None:
    """Entities buried under several container layers still surface."""
    inner = {
        "components": {
            "fixedListComponent": {
                "components": [entity_component(title="Deeply Nested Role")]
            }
        }
    }
    outer = {"components": {"pagedListComponent": {"components": [inner]}}}

    found = list(iter_components(outer))
    assert len(found) == 1
    assert flatten_entity(found[0]).title == "Deeply Nested Role"  # type: ignore[union-attr]


def test_unknown_container_still_yields_its_entities() -> None:
    """A renamed component must degrade, not drop the branch.

    LinkedIn renames union members between releases; the walker descends
    generically rather than pinning an allowlist, so new names keep working.
    """
    payload = {
        "components": {
            "someBrandNewComponentName": {
                "components": [entity_component(title="Survived The Rename")]
            }
        }
    }
    entities = flatten_card_payload(payload)
    assert [e.title for e in entities] == ["Survived The Rename"]


def test_nested_roles_become_children_not_siblings() -> None:
    """Multi-role entries keep their hierarchy so the mapper can expand them."""
    payload = card(
        "experience",
        [
            entity_component(
                title="Acme Corp",
                caption="4 yrs 1 mo",
                children=[
                    entity_component(
                        title="Senior Engineer", caption="Mar 2023 - Present · 1 yr"
                    ),
                    entity_component(
                        title="Engineer", caption="Mar 2021 - Mar 2023 · 2 yrs"
                    ),
                ],
            )
        ],
    )
    entities = flatten_card_payload(payload)

    assert len(entities) == 1, "children must not be hoisted to top level"
    parent = entities[0]
    assert parent.title == "Acme Corp"
    assert [c.title for c in parent.children] == ["Senior Engineer", "Engineer"]


def test_extracts_image_and_link() -> None:
    payload = card(
        "experience",
        [
            entity_component(
                title="Engineer",
                image=vector_image(sizes=(100, 400)),
                link="https://www.linkedin.com/company/acme/?trk=tracking",
                urn="urn:li:fsd_company:1234",
            )
        ],
    )
    e = flatten_card_payload(payload)[0]

    assert e.image is not None
    assert e.image.width == 400, "largest artifact should be the default"
    assert e.image.url.endswith("400_400/0/1700000000000?e=1740000000&v=beta")
    assert e.link == "https://www.linkedin.com/company/acme/", "tracking must be stripped"
    assert e.urn == "urn:li:fsd_company:1234"


def test_empty_entities_are_dropped() -> None:
    payload = card("experience", [entity_component(), entity_component(title="Real")])
    assert [e.title for e in flatten_card_payload(payload)] == ["Real"]


def test_duplicate_description_lines_are_collapsed() -> None:
    """LinkedIn repeats strings for accessibility; they must not double up."""
    payload = card(
        "experience",
        [entity_component(title="Engineer", descriptions=["Same line", "Same line", "Other"])],
    )
    e = flatten_card_payload(payload)[0]
    assert e.texts == ["Same line", "Other"]


def test_malformed_input_does_not_raise() -> None:
    for junk in (None, [], {}, {"components": None}, {"components": {"entityComponent": None}}):
        assert flatten_section(junk) == []
