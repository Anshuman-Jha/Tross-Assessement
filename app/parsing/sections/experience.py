"""Experience section mapper.

Two shapes have to be handled, and conflating them is the classic bug:

**Single role** — the entity *is* the job::

    title    "Senior Software Engineer"
    subtitle "Acme Corp · Full-time"
    caption  "Mar 2021 - Present · 3 yrs 2 mos"
    metadata "London, United Kingdom · Hybrid"

**Multiple roles at one employer** — the entity is the *company*, and each role
is a nested child::

    title    "Acme Corp"                      <- company, not a job title
    caption  "4 yrs 1 mo"                     <- total tenure, not a role range
    children [ "Senior Engineer" (Mar 2023-Present), "Engineer" (Mar 2021-Mar 2023) ]

Flattening the second shape naively produces a fake job whose title is the
company name and loses the promotion history entirely, so children are
expanded into separate entries that inherit the company from their parent.
"""

from __future__ import annotations

from app.models.profile import Experience
from app.parsing.components import FlatEntity
from app.parsing.dates import parse_caption_range
from app.parsing.urn import company_url_from_urn

#: LinkedIn separates facets within one line using a middle dot.
_SEP = "·"

_EMPLOYMENT_TYPES = {
    "full-time", "part-time", "self-employed", "freelance", "contract",
    "internship", "apprenticeship", "seasonal", "temporary", "permanent",
}
_LOCATION_TYPES = {"remote", "hybrid", "on-site", "onsite"}


def parse_experience(entities: list[FlatEntity]) -> list[Experience]:
    out: list[Experience] = []
    for entity in entities:
        if _is_grouped_employer(entity):
            out.extend(_expand_grouped(entity))
        else:
            item = _single(entity)
            if item is not None:
                out.append(item)
    return out


def _is_grouped_employer(entity: FlatEntity) -> bool:
    """True when this entity groups several roles at one company.

    The tell is children that carry their own date captions: a plain
    description sub-component never parses as a date range.
    """
    if not entity.children:
        return False
    return any(
        child.title and parse_caption_range(child.caption) is not None
        for child in entity.children
    )


def _expand_grouped(parent: FlatEntity) -> list[Experience]:
    company = parent.title
    company_url = parent.link or company_url_from_urn(parent.urn)
    out: list[Experience] = []
    for child in parent.children:
        if not child.title:
            continue
        dates = parse_caption_range(child.caption)
        location, location_type = _split_location(child.metadata)
        # In grouped form the child's subtitle carries the employment type,
        # since the company is already established by the parent.
        employment_type = _employment_type_from(child.subtitle)
        out.append(
            Experience(
                title=child.title,
                company=company,
                company_url=company_url,
                company_urn=parent.urn,
                company_logo=parent.image,
                employment_type=employment_type,
                location=location,
                location_type=location_type,
                dates=dates,
                is_current=bool(dates and dates.is_current),
                description=child.description,
            )
        )
    return out


def _single(entity: FlatEntity) -> Experience | None:
    if not entity.title and not entity.subtitle:
        return None
    company, employment_type = _split_company(entity.subtitle)
    dates = parse_caption_range(entity.caption)
    location, location_type = _split_location(entity.metadata)
    return Experience(
        title=entity.title,
        company=company,
        company_url=entity.link or company_url_from_urn(entity.urn),
        company_urn=entity.urn,
        company_logo=entity.image,
        employment_type=employment_type,
        location=location,
        location_type=location_type,
        dates=dates,
        is_current=bool(dates and dates.is_current),
        description=entity.description,
    )


def _split_company(subtitle: str | None) -> tuple[str | None, str | None]:
    """``"Acme Corp · Full-time"`` -> ``("Acme Corp", "Full-time")``."""
    if not subtitle:
        return None, None
    parts = [p.strip() for p in subtitle.split(_SEP) if p.strip()]
    if not parts:
        return None, None
    company = parts[0]
    employment_type = next(
        (p for p in parts[1:] if p.lower() in _EMPLOYMENT_TYPES),
        parts[1] if len(parts) > 1 else None,
    )
    return company, employment_type


def _employment_type_from(subtitle: str | None) -> str | None:
    if not subtitle:
        return None
    for part in (p.strip() for p in subtitle.split(_SEP)):
        if part.lower() in _EMPLOYMENT_TYPES:
            return part
    return subtitle.strip() or None


def _split_location(metadata: str | None) -> tuple[str | None, str | None]:
    """``"London, UK · Hybrid"`` -> ``("London, UK", "Hybrid")``."""
    if not metadata:
        return None, None
    parts = [p.strip() for p in metadata.split(_SEP) if p.strip()]
    if not parts:
        return None, None
    location_type = next((p for p in parts if p.lower() in _LOCATION_TYPES), None)
    location = next((p for p in parts if p.lower() not in _LOCATION_TYPES), None)
    return location, location_type
