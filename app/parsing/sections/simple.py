"""Mappers for the sections whose shape is a straight field rename.

These exist as one module rather than ten files because, once the component
walker has flattened the tree, each is genuinely a handful of lines. Splitting
them further would add navigation cost without adding clarity.
"""

from __future__ import annotations

import re

from app.models.profile import (
    Certification,
    Course,
    Honor,
    Language,
    Organization,
    Patent,
    Project,
    Publication,
    Skill,
    TestScore,
    Volunteering,
)
from app.parsing.components import FlatEntity
from app.parsing.dates import parse_caption_range

_SEP = "·"

_ENDORSEMENT_RE = re.compile(r"(\d[\d,]*)\s+endorsement", re.IGNORECASE)
_ISSUED_RE = re.compile(r"issued\s+(?P<value>.+?)(?:\s*[·|]|$)", re.IGNORECASE)
_EXPIRES_RE = re.compile(r"expir\w*\s+(?P<value>.+?)(?:\s*[·|]|$)", re.IGNORECASE)
_CREDENTIAL_ID_RE = re.compile(r"credential\s+id\s*:?\s*(?P<value>\S+)", re.IGNORECASE)


# ----------------------------------------------------------------- skills


def parse_skills(entities: list[FlatEntity]) -> list[Skill]:
    out: list[Skill] = []
    seen: set[str] = set()
    for entity in entities:
        name = (entity.title or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        insights = [t for t in entity.texts if t]
        out.append(
            Skill(
                name=name,
                endorsement_count=_endorsements(entity),
                insights=insights,
            )
        )
    return out


def _endorsements(entity: FlatEntity) -> int | None:
    for candidate in (*entity.texts, entity.subtitle or "", entity.caption or ""):
        if match := _ENDORSEMENT_RE.search(candidate):
            try:
                return int(match.group(1).replace(",", ""))
            except ValueError:  # pragma: no cover - regex guarantees digits
                return None
    return None


# --------------------------------------------------------- certifications


def parse_certifications(entities: list[FlatEntity]) -> list[Certification]:
    out: list[Certification] = []
    for entity in entities:
        if not entity.title:
            continue
        caption = entity.caption or ""
        issued = _ISSUED_RE.search(caption)
        expires = _EXPIRES_RE.search(caption)
        # Without an "Issued" label the caption is just the date itself.
        issue_date = issued.group("value").strip() if issued else (caption.strip() or None)
        blob = " ".join([entity.metadata or "", *entity.texts])
        credential = _CREDENTIAL_ID_RE.search(blob)
        out.append(
            Certification(
                name=entity.title,
                issuer=(entity.subtitle or "").strip() or None,
                issuer_logo=entity.image,
                issue_date=issue_date,
                expiration_date=expires.group("value").strip() if expires else None,
                credential_id=credential.group("value").strip() if credential else None,
                credential_url=entity.link,
            )
        )
    return out


# -------------------------------------------------------------- languages


def parse_languages(entities: list[FlatEntity]) -> list[Language]:
    out: list[Language] = []
    for entity in entities:
        name = (entity.title or "").strip()
        if not name:
            continue
        proficiency = (entity.caption or entity.subtitle or "").strip() or None
        out.append(Language(name=name, proficiency=proficiency))
    return out


# --------------------------------------------------------------- projects


def parse_projects(entities: list[FlatEntity]) -> list[Project]:
    return [
        Project(
            name=e.title,
            description=e.description,
            url=e.link,
            dates=parse_caption_range(e.caption),
        )
        for e in entities
        if e.title
    ]


def parse_publications(entities: list[FlatEntity]) -> list[Publication]:
    out: list[Publication] = []
    for e in entities:
        if not e.title:
            continue
        publisher, date = _split_pair(e.subtitle)
        out.append(
            Publication(
                name=e.title,
                publisher=publisher,
                date=date or (e.caption or None),
                description=e.description,
                url=e.link,
            )
        )
    return out


def parse_honors(entities: list[FlatEntity]) -> list[Honor]:
    out: list[Honor] = []
    for e in entities:
        if not e.title:
            continue
        issuer, date = _split_pair(e.subtitle)
        out.append(
            Honor(
                title=e.title,
                issuer=issuer,
                date=date or (e.caption or None),
                description=e.description,
            )
        )
    return out


def parse_volunteering(entities: list[FlatEntity]) -> list[Volunteering]:
    return [
        Volunteering(
            role=e.title,
            organization=(e.subtitle or "").strip() or None,
            cause=(e.metadata or "").strip() or None,
            dates=parse_caption_range(e.caption),
            description=e.description,
        )
        for e in entities
        if e.title or e.subtitle
    ]


def parse_courses(entities: list[FlatEntity]) -> list[Course]:
    return [
        Course(name=e.title, number=(e.subtitle or "").strip() or None)
        for e in entities
        if e.title
    ]


def parse_patents(entities: list[FlatEntity]) -> list[Patent]:
    out: list[Patent] = []
    for e in entities:
        if not e.title:
            continue
        number, date = _split_pair(e.subtitle)
        out.append(
            Patent(
                title=e.title,
                number=number,
                date=date or (e.caption or None),
                description=e.description,
                url=e.link,
            )
        )
    return out


def parse_organizations(entities: list[FlatEntity]) -> list[Organization]:
    return [
        Organization(
            name=e.title,
            role=(e.subtitle or "").strip() or None,
            dates=parse_caption_range(e.caption),
            description=e.description,
        )
        for e in entities
        if e.title
    ]


def parse_test_scores(entities: list[FlatEntity]) -> list[TestScore]:
    out: list[TestScore] = []
    for e in entities:
        if not e.title:
            continue
        score, date = _split_pair(e.subtitle)
        out.append(
            TestScore(
                name=e.title,
                score=score,
                date=date or (e.caption or None),
                description=e.description,
            )
        )
    return out


def _split_pair(subtitle: str | None) -> tuple[str | None, str | None]:
    """Split ``"IEEE · Mar 2020"`` into its two middle-dot separated halves."""
    if not subtitle:
        return None, None
    parts = [p.strip() for p in subtitle.split(_SEP) if p.strip()]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]
