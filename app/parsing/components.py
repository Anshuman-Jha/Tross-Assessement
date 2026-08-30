"""A single generic reader for Voyager's GraphQL component trees.

Every profile section — experience, education, certifications, projects — comes
back as the *same* recursive component structure. LinkedIn renders them with
one generic UI, so they share one generic payload::

    topComponents: [
      { components: { fixedListComponent: { components: [
          { components: { entityComponent: {
              image:     {...},
              titleV2:   { text: { text: "Senior Engineer" } },
              subtitle:  { text: "Acme Corp · Full-time" },
              caption:   { text: "Mar 2021 - Present · 3 yrs 2 mos" },
              metadata:  { text: "London, United Kingdom" },
              textActionTarget: "https://www.linkedin.com/company/acme/",
              subComponents: { components: [ ...description, nested roles... ] }
          }}}
      ]}}}
    ]

The consequence worth exploiting: **one walker plus a thin per-section mapper**
replaces ten bespoke parsers. Each section module receives the flattened
``(title, subtitle, caption, metadata, description, image, link, children)``
tuple and only decides what those slots *mean* for it — for experience the
subtitle is the company, for education it is the degree.

Nesting carries real meaning too. When someone held several roles at one
employer, LinkedIn emits the company as the outer entity and the roles as
``children``; flattening that away loses the promotion history.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from app.parsing.collection import dig, text_of
from app.parsing.images import Image, parse_vector_image

#: Union members that hold a list of further components.
_CONTAINER_KEYS = (
    "fixedListComponent",
    "pagedListComponent",
    "carouselComponent",
    "sectionComponent",
    "tabComponent",
    "listComponent",
)

#: Union members that hold a single renderable string.
_TEXT_KEYS = ("textComponent", "insightComponent", "headerComponent", "subtitleComponent")

MAX_WALK_DEPTH = 18


@dataclass
class FlatEntity:
    """One flattened ``entityComponent``."""

    title: str | None = None
    subtitle: str | None = None
    caption: str | None = None
    metadata: str | None = None
    #: Free text pulled from subComponents (job descriptions, activities).
    description: str | None = None
    #: Every text block found beneath this entity, in document order.
    texts: list[str] = field(default_factory=list)
    image: Image | None = None
    #: Destination of the entity's primary link (company page, credential URL).
    link: str | None = None
    urn: str | None = None
    #: Nested entities — e.g. individual roles under one employer.
    children: list[FlatEntity] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any((self.title, self.subtitle, self.caption, self.metadata, self.description))


# --------------------------------------------------------------------- walking


def iter_components(node: object, depth: int = 0) -> Iterator[dict[str, Any]]:
    """Yield every ``entityComponent`` dict beneath ``node``, in order.

    Containers are descended into; nested entities inside an entity's
    ``subComponents`` are *not* yielded here, since :func:`flatten_entity`
    claims those as children.
    """
    if depth > MAX_WALK_DEPTH:
        return

    if isinstance(node, list):
        for item in node:
            yield from iter_components(item, depth + 1)
        return

    if not isinstance(node, dict):
        return

    components = node.get("components")

    if isinstance(components, dict):
        entity = components.get("entityComponent")
        if isinstance(entity, dict):
            yield entity
            return
        for key in _CONTAINER_KEYS:
            container = components.get(key)
            if isinstance(container, dict):
                yield from iter_components(container.get("components"), depth + 1)
                return
        # An unrecognised union member: descend generically rather than
        # dropping the branch, so a renamed component still yields its content.
        yield from iter_components(list(components.values()), depth + 1)
        return

    if isinstance(components, list):
        yield from iter_components(components, depth + 1)
        return

    if isinstance(node.get("entityComponent"), dict):
        yield node["entityComponent"]
        return

    for key in ("topComponents", "elements", "items", *_CONTAINER_KEYS):
        if key in node:
            yield from iter_components(node[key], depth + 1)


def flatten_entity(entity: object, depth: int = 0) -> FlatEntity | None:
    """Collapse one ``entityComponent`` into a :class:`FlatEntity`."""
    if not isinstance(entity, dict) or depth > 6:
        return None

    flat = FlatEntity(
        title=text_of(entity.get("titleV2") or entity.get("title")),
        subtitle=text_of(entity.get("subtitle")),
        caption=text_of(entity.get("caption")),
        metadata=text_of(entity.get("metadata")),
        image=_entity_image(entity),
        link=_entity_link(entity),
        urn=_entity_urn(entity),
    )

    texts, children = _read_sub_components(entity.get("subComponents"), depth)
    flat.texts = texts
    flat.children = children
    if texts:
        flat.description = "\n".join(texts).strip() or None

    return flat


def _read_sub_components(
    sub: object, depth: int
) -> tuple[list[str], list[FlatEntity]]:
    """Split an entity's subComponents into loose text and nested entities."""
    texts: list[str] = []
    children: list[FlatEntity] = []

    def walk(node: object, d: int) -> None:
        if d > MAX_WALK_DEPTH or node is None:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, d + 1)
            return
        if not isinstance(node, dict):
            return

        components = node.get("components")
        if isinstance(components, dict):
            nested_entity = components.get("entityComponent")
            if isinstance(nested_entity, dict):
                child = flatten_entity(nested_entity, depth + 1)
                if child and not child.is_empty():
                    children.append(child)
                return
            for key in _TEXT_KEYS:
                block = components.get(key)
                if isinstance(block, dict):
                    value = text_of(block.get("text") or block)
                    if value:
                        texts.append(value)
                    return
            for key in _CONTAINER_KEYS:
                container = components.get(key)
                if isinstance(container, dict):
                    walk(container.get("components"), d + 1)
                    return
            walk(list(components.values()), d + 1)
            return

        if isinstance(components, list):
            walk(components, d + 1)
            return

        for value in node.values():
            walk(value, d + 1)

    walk(sub, 0)
    # Preserve order while removing the duplicates LinkedIn emits when the same
    # string is used for both display and accessibility.
    deduped = list(dict.fromkeys(t for t in texts if t))
    return deduped, children


def _entity_image(entity: dict[str, Any]) -> Image | None:
    """Find the entity's logo/avatar.

    The GraphQL tier buries it under ``image.attributes[].detailData``, while
    other shapes hold it directly; :func:`parse_vector_image` unwraps both, so
    each candidate is simply handed over until one yields.
    """
    for key in ("image", "logo", "icon", "picture"):
        candidate = entity.get(key)
        if candidate is None:
            continue
        found = parse_vector_image(candidate)
        if found:
            return found
    return None


def _entity_link(entity: dict[str, Any]) -> str | None:
    for key in ("textActionTarget", "actionTarget", "navigationUrl", "target"):
        value = entity.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return _strip_tracking(value)
    nested = dig(entity, "navigationAction", "actionTarget")
    if isinstance(nested, str) and nested.startswith("http"):
        return _strip_tracking(nested)
    return None


def _entity_urn(entity: dict[str, Any]) -> str | None:
    for key in ("entityUrn", "trackingUrn", "targetUrn"):
        value = entity.get(key)
        if isinstance(value, str) and value.startswith("urn:li:"):
            return value
    return None


def _strip_tracking(url: str) -> str:
    """Drop LinkedIn's tracking query string from an outbound link."""
    return url.split("?")[0] if "linkedin.com" in url else url


def flatten_section(node: object) -> list[FlatEntity]:
    """Flatten a bare component subtree into its top-level entities."""
    out: list[FlatEntity] = []
    for entity in iter_components(node):
        flat = flatten_entity(entity)
        if flat and not flat.is_empty():
            out.append(flat)
    return out


def flatten_card_payload(payload: object) -> list[FlatEntity]:
    """Flatten a complete normalised profile-card response.

    This is the entry point the fetcher uses. A real response wraps its cards in
    the normalised envelope — the component tree lives on card entities inside
    ``included[]``, not at the top level — so the cards are located first and
    their ``topComponents`` flattened.
    """
    from app.parsing.collection import EntityIndex

    index = EntityIndex(payload)
    cards = [e for e in index.entities if "topComponents" in e]
    if not cards:
        # A bare subtree, or an envelope we did not recognise: walk it directly.
        return flatten_section(payload)

    out: list[FlatEntity] = []
    for card in cards:
        out.extend(flatten_section(card.get("topComponents")))
    return out
