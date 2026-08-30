"""Education section mapper.

    title    "Massachusetts Institute of Technology"
    subtitle "Bachelor of Science - BS, Computer Science"
    caption  "2015 - 2019"
    texts    ["Grade: 3.9", "Activities and societies: Robotics Club"]

The subtitle packs degree and field into one comma-separated string. It is
split on the *last* comma rather than the first, because degree names routinely
contain commas of their own ("Bachelor of Science - BS, Honours, Physics"),
whereas the field of study rarely does.
"""

from __future__ import annotations

import re

from app.models.profile import Education
from app.parsing.components import FlatEntity
from app.parsing.dates import parse_caption_range
from app.parsing.urn import company_url_from_urn

_GRADE_RE = re.compile(r"^\s*grade\s*:\s*(?P<value>.+)$", re.IGNORECASE)
_ACTIVITIES_RE = re.compile(
    r"^\s*activities(?:\s+and\s+societies)?\s*:\s*(?P<value>.+)$", re.IGNORECASE
)


def parse_education(entities: list[FlatEntity]) -> list[Education]:
    out: list[Education] = []
    for entity in entities:
        if not entity.title and not entity.subtitle:
            continue
        degree, field_of_study = _split_degree(entity.subtitle)
        grade, activities, description = _split_texts(entity.texts)
        out.append(
            Education(
                school=entity.title,
                school_url=entity.link or company_url_from_urn(entity.urn),
                school_urn=entity.urn,
                school_logo=entity.image,
                degree=degree,
                field_of_study=field_of_study,
                grade=grade,
                dates=parse_caption_range(entity.caption),
                description=description,
                activities=activities,
            )
        )
    return out


def _split_degree(subtitle: str | None) -> tuple[str | None, str | None]:
    if not subtitle:
        return None, None
    text = subtitle.strip()
    if "," not in text:
        return text or None, None
    head, _, tail = text.rpartition(",")
    return head.strip() or None, tail.strip() or None


def _split_texts(texts: list[str]) -> tuple[str | None, str | None, str | None]:
    """Pull the labelled Grade/Activities lines out of the free text."""
    grade: str | None = None
    activities: str | None = None
    remainder: list[str] = []

    for line in texts:
        if match := _GRADE_RE.match(line):
            grade = match.group("value").strip()
        elif match := _ACTIVITIES_RE.match(line):
            activities = match.group("value").strip()
        else:
            remainder.append(line)

    description = "\n".join(remainder).strip() or None
    return grade, activities, description
