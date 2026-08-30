"""Tier 2: the legacy ``profileView`` shape.

The older REST endpoint predates the component-tree design. Instead of rendered
strings it returns *typed* entities with real field names and structured
dates::

    { "$type": "com.linkedin.voyager.identity.profile.Position",
      "title": "Senior Engineer",
      "companyName": "Acme Corp",
      "locationName": "London, United Kingdom",
      "timePeriod": { "startDate": {"month": 3, "year": 2021} } }

Which makes this tier *higher* fidelity than the GraphQL one where it works —
dates are structured rather than parsed out of localised captions. It ranks
second only because LinkedIn has been retiring it, and it is dark for many
accounts. When it does answer, its output is the most trustworthy of the three.
"""

from __future__ import annotations

from typing import Any

from app.models.profile import (
    Certification,
    Course,
    Education,
    Experience,
    Honor,
    Language,
    Organization,
    Patent,
    Profile,
    Project,
    Publication,
    Skill,
    TestScore,
    Volunteering,
)
from app.parsing.collection import EntityIndex, dig, text_of
from app.parsing.dates import parse_structured_range
from app.parsing.images import parse_vector_image
from app.parsing.sections.basics import parse_basics
from app.parsing.urn import company_url_from_urn


def parse_rest_profile(payload: object, *, public_id: str | None = None) -> Profile:
    index = EntityIndex(payload)
    return Profile(
        basics=parse_basics(index, public_id=public_id),
        experience=_positions(index),
        education=_educations(index),
        skills=_skills(index),
        certifications=_certifications(index),
        languages=_languages(index),
        projects=_projects(index),
        publications=_publications(index),
        honors=_honors(index),
        volunteering=_volunteering(index),
        courses=_courses(index),
        patents=_patents(index),
        organizations=_organizations(index),
        test_scores=_test_scores(index),
    )


def _positions(index: EntityIndex) -> list[Experience]:
    out: list[Experience] = []
    for entity in index.by_type("profile.Position", "Position"):
        dates = parse_structured_range(entity.get("timePeriod") or entity)
        company_urn = entity.get("companyUrn") or dig(entity, "company", "entityUrn")
        out.append(
            Experience(
                title=text_of(entity.get("title")),
                company=text_of(entity.get("companyName")),
                company_url=company_url_from_urn(company_urn),
                company_urn=company_urn if isinstance(company_urn, str) else None,
                company_logo=parse_vector_image(dig(entity, "company", "miniCompany", "logo")),
                employment_type=text_of(entity.get("employmentTypeUrn")),
                location=text_of(entity.get("locationName")),
                dates=dates,
                is_current=bool(dates and dates.is_current),
                description=text_of(entity.get("description")),
            )
        )
    return out


def _educations(index: EntityIndex) -> list[Education]:
    out: list[Education] = []
    for entity in index.by_type("profile.Education", "Education"):
        school_urn = entity.get("schoolUrn") or dig(entity, "school", "entityUrn")
        out.append(
            Education(
                school=text_of(entity.get("schoolName")),
                school_url=company_url_from_urn(school_urn),
                school_urn=school_urn if isinstance(school_urn, str) else None,
                school_logo=parse_vector_image(dig(entity, "school", "logo")),
                degree=text_of(entity.get("degreeName")),
                field_of_study=text_of(entity.get("fieldOfStudy")),
                grade=text_of(entity.get("grade")),
                dates=parse_structured_range(entity.get("timePeriod") or entity),
                description=text_of(entity.get("description")),
                activities=text_of(entity.get("activities")),
            )
        )
    return out


def _skills(index: EntityIndex) -> list[Skill]:
    out: list[Skill] = []
    seen: set[str] = set()
    for entity in index.by_type("profile.Skill", "Skill"):
        name = text_of(entity.get("name"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        count = entity.get("endorsementCount")
        out.append(
            Skill(name=name, endorsement_count=count if isinstance(count, int) else None)
        )
    return out


def _certifications(index: EntityIndex) -> list[Certification]:
    out: list[Certification] = []
    for entity in index.by_type("profile.Certification", "Certification"):
        dates = parse_structured_range(entity.get("timePeriod") or entity)
        out.append(
            Certification(
                name=text_of(entity.get("name")),
                issuer=text_of(entity.get("authority")),
                issue_date=dates.start.to_iso() if dates and dates.start else None,
                expiration_date=dates.end.to_iso() if dates and dates.end else None,
                credential_id=text_of(entity.get("licenseNumber")),
                credential_url=text_of(entity.get("url")),
            )
        )
    return out


def _languages(index: EntityIndex) -> list[Language]:
    out: list[Language] = []
    for entity in index.by_type("profile.Language", "Language"):
        name = text_of(entity.get("name"))
        if name:
            out.append(Language(name=name, proficiency=text_of(entity.get("proficiency"))))
    return out


def _projects(index: EntityIndex) -> list[Project]:
    return [
        Project(
            name=text_of(e.get("title")),
            description=text_of(e.get("description")),
            url=text_of(e.get("url")),
            dates=parse_structured_range(e.get("timePeriod") or e),
        )
        for e in index.by_type("profile.Project", "Project")
        if text_of(e.get("title"))
    ]


def _publications(index: EntityIndex) -> list[Publication]:
    return [
        Publication(
            name=text_of(e.get("name")),
            publisher=text_of(e.get("publisher")),
            date=_iso_of(e.get("date")),
            description=text_of(e.get("description")),
            url=text_of(e.get("url")),
        )
        for e in index.by_type("profile.Publication", "Publication")
        if text_of(e.get("name"))
    ]


def _honors(index: EntityIndex) -> list[Honor]:
    return [
        Honor(
            title=text_of(e.get("title")),
            issuer=text_of(e.get("issuer")),
            date=_iso_of(e.get("issueDate")),
            description=text_of(e.get("description")),
        )
        for e in index.by_type("profile.Honor", "Honor")
        if text_of(e.get("title"))
    ]


def _volunteering(index: EntityIndex) -> list[Volunteering]:
    return [
        Volunteering(
            role=text_of(e.get("role")),
            organization=text_of(e.get("companyName")),
            cause=text_of(e.get("cause")),
            dates=parse_structured_range(e.get("timePeriod") or e),
            description=text_of(e.get("description")),
        )
        for e in index.by_type("VolunteerExperience")
        if text_of(e.get("role")) or text_of(e.get("companyName"))
    ]


def _courses(index: EntityIndex) -> list[Course]:
    return [
        Course(name=text_of(e.get("name")), number=text_of(e.get("number")))
        for e in index.by_type("profile.Course", "Course")
        if text_of(e.get("name"))
    ]


def _patents(index: EntityIndex) -> list[Patent]:
    return [
        Patent(
            title=text_of(e.get("title")),
            number=text_of(e.get("number")),
            date=_iso_of(e.get("issueDate") or e.get("filingDate")),
            description=text_of(e.get("description")),
            url=text_of(e.get("url")),
        )
        for e in index.by_type("profile.Patent", "Patent")
        if text_of(e.get("title"))
    ]


def _organizations(index: EntityIndex) -> list[Organization]:
    return [
        Organization(
            name=text_of(e.get("name")),
            role=text_of(e.get("position")),
            dates=parse_structured_range(e.get("timePeriod") or e),
            description=text_of(e.get("description")),
        )
        for e in index.by_type("profile.Organization", "Organization")
        if text_of(e.get("name"))
    ]


def _test_scores(index: EntityIndex) -> list[TestScore]:
    return [
        TestScore(
            name=text_of(e.get("name")),
            score=text_of(e.get("score")),
            date=_iso_of(e.get("date")),
            description=text_of(e.get("description")),
        )
        for e in index.by_type("profile.TestScore", "TestScore")
        if text_of(e.get("name"))
    ]


def _iso_of(raw: Any) -> str | None:
    """Render a bare ``{year, month, day}`` object as an ISO-ish string."""
    from app.parsing.dates import parse_partial_date

    parsed = parse_partial_date(raw)
    return parsed.to_iso() if parsed else None
