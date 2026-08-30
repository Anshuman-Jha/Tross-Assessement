"""Builders for synthetic Voyager payloads.

These mirror the component structure documented in
``app/parsing/components.py``. They let the parser suite run entirely offline
and in CI, with no LinkedIn session and no network.

They are a *model* of the upstream shape, not a recording of it.
``scripts/recon.py`` captures real sanitised responses into
``tests/fixtures/``; where a recording and a factory disagree, the recording is
the authority and these builders get corrected.
"""

from __future__ import annotations

from typing import Any


def text(value: str | None) -> dict[str, Any] | None:
    """Voyager's attributed-text wrapper: ``{"text": {"text": "..."}}``."""
    if value is None:
        return None
    return {"text": value}


def vector_image(
    root: str = "https://media.licdn.com/dms/image/ABC/",
    sizes: tuple[int, ...] = (100, 400),
) -> dict[str, Any]:
    return {
        "rootUrl": root,
        "artifacts": [
            {
                "width": s,
                "height": s,
                "fileIdentifyingUrlPathSegment": f"{s}_{s}/0/1700000000000?e=1740000000&v=beta",
            }
            for s in sizes
        ],
    }


def entity_component(
    *,
    title: str | None = None,
    subtitle: str | None = None,
    caption: str | None = None,
    metadata: str | None = None,
    link: str | None = None,
    urn: str | None = None,
    image: dict[str, Any] | None = None,
    descriptions: list[str] | None = None,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One ``entityComponent``, wrapped as it appears inside a component list."""
    sub_components: list[dict[str, Any]] = []
    for line in descriptions or []:
        sub_components.append({"components": {"textComponent": {"text": {"text": line}}}})
    for child in children or []:
        sub_components.append(child)

    entity: dict[str, Any] = {
        "titleV2": text(title),
        "subtitle": text(subtitle),
        "caption": text(caption),
        "metadata": text(metadata),
    }
    if link:
        entity["textActionTarget"] = link
    if urn:
        entity["entityUrn"] = urn
    if image:
        entity["image"] = image
    if sub_components:
        entity["subComponents"] = {"components": sub_components}

    return {"components": {"entityComponent": entity}}


def card(
    section: str, entities: list[dict[str, Any]], profile_id: str = "ACoAAATEST"
) -> dict[str, Any]:
    """A full normalised profile-card response for one section."""
    card_urn = f"urn:li:fsd_profileCard:(urn:li:fsd_profile:{profile_id},{section.upper()},en_US)"
    return {
        "data": {
            "data": {
                "identityDashProfileCardsByInitialCards": {"*elements": [card_urn]}
            }
        },
        "included": [
            {
                "entityUrn": card_urn,
                "$type": "com.linkedin.voyager.dash.identity.profile.tetris.Card",
                "topComponents": [
                    {"components": {"headerComponent": {"title": text(section.title())}}},
                    {"components": {"fixedListComponent": {"components": entities}}},
                ],
            }
        ],
    }
