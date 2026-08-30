"""Parsing the modern ``identity/dash/profiles`` response.

This is the endpoint that actually works. The legacy ``profileView`` returns
**410 Gone**, and the GraphQL card endpoints need a ``queryId`` that LinkedIn
rotates — but the dash finder is a plain Rest.li query
(``?q=memberIdentity&memberIdentity=<slug>&decorationId=…``), so it needs no
persisted-query hash and cannot be broken by rotation.

The ``decorationId`` is the interesting part: it is a projection telling
LinkedIn how much of the object graph to inline. With
``FullProfileWithEntities`` one request returns the profile *and* its positions,
educations, companies and schools together.

The response is the normalised collection format, so entities arrive flat in
``included[]`` and reference each other by URN::

    { "$type": "…profile.Position", "title": "Co-chair",
      "companyName": "Gates Foundation", "*company": "urn:li:fsd_company:8736",
      "dateRange": { "start": { "year": 2000 } } }

Unlike the GraphQL card tier, values here are **typed fields rather than
rendered strings** — dates are ``{year, month}`` objects, not localised
captions like "Mar 2021 - Present · 3 yrs". That makes this the highest
fidelity source of the three: nothing has to be recovered from display text.

Verified against a live capture of ``linkedin.com/in/williamhgates``.
"""

from __future__ import annotations

from typing import Any

from app.models.profile import (
    Basics,
    Certification,
    Course,
    Education,
    Experience,
    Honor,
    Language,
    Location,
    Organization,
    Patent,
    Profile,
    Project,
    Publication,
    Skill,
    TestScore,
    Volunteering,
)
from app.observability.logging import get_logger
from app.parsing.collection import EntityIndex, dig
from app.parsing.dates import parse_structured_range
from app.parsing.images import parse_vector_image
from app.parsing.urn import company_url_from_urn

logger = get_logger(__name__)


def parse_dash_profile(payload: object, *, public_id: str | None = None) -> Profile:
    """Build a :class:`Profile` from a dash profiles response."""
    index = EntityIndex(payload)
    profile_entity = _subject(index)

    profile = Profile(basics=_basics(index, profile_entity, public_id))
    if profile_entity is None:
        return profile

    profile.experience = _experience(index)
    profile.education = _education(index)
    profile.skills = _skills(index)
    profile.certifications = _certifications(index)
    profile.languages = _languages(index)
    profile.projects = _projects(index)
    profile.publications = _publications(index)
    profile.honors = _honors(index)
    profile.volunteering = _volunteering(index)
    profile.courses = _courses(index)
    profile.patents = _patents(index)
    profile.organizations = _organizations(index)
    profile.test_scores = _test_scores(index)

    logger.info(
        "dash.parsed",
        public_id=profile.basics.public_identifier,
        experience=len(profile.experience),
        education=len(profile.education),
        skills=len(profile.skills),
    )
    return profile


def _subject(index: EntityIndex) -> dict[str, Any] | None:
    """The Profile entity for the person being looked up.

    A response can contain several profile entities; the subject is the one
    carrying identity fields.
    """
    candidates = index.by_type("identity.profile.Profile", "profile.Profile")
    for entity in candidates:
        if entity.get("firstName") or entity.get("headline") or entity.get("publicIdentifier"):
            return entity
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------- basics


def _basics(
    index: EntityIndex, entity: dict[str, Any] | None, public_id: str | None
) -> Basics:
    if entity is None:
        return Basics(public_identifier=public_id)

    return Basics(
        first_name=_text(entity.get("firstName")),
        last_name=_text(entity.get("lastName")),
        full_name=" ".join(
            p for p in (_text(entity.get("firstName")), _text(entity.get("lastName"))) if p
        )
        or None,
        headline=_text(entity.get("headline")),
        # `summary` is the profile's "About" section.
        about=_text(entity.get("summary")),
        public_identifier=_text(entity.get("publicIdentifier")) or public_id,
        profile_url=(
            f"https://www.linkedin.com/in/{entity['publicIdentifier']}"
            if entity.get("publicIdentifier")
            else None
        ),
        location=_location(index, entity),
        industry=_industry(index, entity),
        is_premium=_bool(entity.get("premium")),
        is_influencer=_bool(entity.get("influencer")),
        profile_picture=parse_vector_image(entity.get("profilePicture")),
        background_image=parse_vector_image(entity.get("backgroundPicture")),
    )


def _location(index: EntityIndex, entity: dict[str, Any]) -> Location:
    """Assemble the display location.

    The country code sits on the profile's own ``location`` object, while the
    human-readable place name lives on a referenced ``Geo`` entity.
    """
    country_code = dig(entity, "location", "countryCode")
    full = _text(entity.get("locationName"))

    geo_urn = entity.get("*geoLocation") or dig(entity, "geoLocation", "*geo")
    if isinstance(geo_urn, str):
        geo = index.get(geo_urn)
        if geo:
            full = full or _text(geo.get("defaultLocalizedName")) or _text(geo.get("name"))
    if not full:
        for geo in index.by_type("common.Geo", "Geo"):
            name = _text(geo.get("defaultLocalizedName")) or _text(geo.get("name"))
            if name:
                full = name
                break

    city = full.split(",")[0].strip() if full and "," in full else None
    country = full.split(",")[-1].strip() if full and "," in full else None
    return Location(
        full=full,
        city=city,
        country=country,
        country_code=country_code if isinstance(country_code, str) else None,
    )


def _industry(index: EntityIndex, entity: dict[str, Any]) -> str | None:
    urn = entity.get("industryUrn") or entity.get("*industry")
    if isinstance(urn, str):
        found = index.get(urn)
        if found:
            return _text(found.get("name"))
    for ind in index.by_type("common.Industry", "Industry"):
        name = _text(ind.get("name"))
        if name:
            return name
    return None


# ------------------------------------------------------------------ experience


def _company_logo(index: EntityIndex, urn: object) -> Any:
    """Resolve a company/school URN to its logo image."""
    if not isinstance(urn, str):
        return None
    entity = index.get(urn)
    if not entity:
        return None
    return parse_vector_image(entity.get("logo")) or parse_vector_image(entity.get("image"))


def _experience(index: EntityIndex) -> list[Experience]:
    """Read every Position.

    ``PositionGroup`` entities exist to group several roles at one employer for
    display, but each role is also emitted as its own ``Position``. Reading
    Positions directly therefore yields the complete history without having to
    walk the grouping — and without the risk of turning a company into a fake
    job title, which is the classic bug in the card-based tier.
    """
    out: list[Experience] = []
    for pos in index.by_type("profile.Position", "Position"):
        dates = parse_structured_range(pos.get("dateRange"))
        company_urn = pos.get("companyUrn") or pos.get("*company")
        out.append(
            Experience(
                title=_text(pos.get("title")),
                company=_text(pos.get("companyName")),
                company_url=company_url_from_urn(company_urn),
                company_urn=company_urn if isinstance(company_urn, str) else None,
                company_logo=_company_logo(index, company_urn),
                employment_type=_text(pos.get("employmentTypeUrn")),
                location=_text(pos.get("locationName")),
                dates=dates,
                is_current=bool(dates and dates.is_current),
                description=_text(pos.get("description")),
            )
        )
    out.sort(key=_recency, reverse=True)
    return out


def _recency(item: Experience | Education) -> tuple[int, int]:
    """Sort key placing current and most recent entries first."""
    dates = item.dates
    if not dates or not dates.start:
        return (0, 0)
    return (dates.start.year or 0, dates.start.month or 0)


def _education(index: EntityIndex) -> list[Education]:
    out: list[Education] = []
    for edu in index.by_type("profile.Education", "Education"):
        school_urn = edu.get("schoolUrn") or edu.get("*school")
        out.append(
            Education(
                school=_text(edu.get("schoolName")),
                school_url=company_url_from_urn(school_urn),
                school_urn=school_urn if isinstance(school_urn, str) else None,
                school_logo=_company_logo(index, school_urn)
                or _company_logo(index, edu.get("companyUrn")),
                degree=_text(edu.get("degreeName")),
                field_of_study=_text(edu.get("fieldOfStudy")),
                grade=_text(edu.get("grade")),
                dates=parse_structured_range(edu.get("dateRange")),
                description=_text(edu.get("description")),
                activities=_text(edu.get("activities")),
            )
        )
    out.sort(key=_recency, reverse=True)
    return out


# ------------------------------------------------------------- other sections


def _skills(index: EntityIndex) -> list[Skill]:
    out: list[Skill] = []
    seen: set[str] = set()
    for s in index.by_type("profile.Skill", "Skill"):
        name = _text(s.get("name"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        count = s.get("endorsementCount") or dig(s, "endorsedSkill", "endorsementCount")
        out.append(
            Skill(name=name, endorsement_count=count if isinstance(count, int) else None)
        )
    return out


def _certifications(index: EntityIndex) -> list[Certification]:
    out: list[Certification] = []
    for c in index.by_type("profile.Certification", "Certification"):
        dates = parse_structured_range(c.get("dateRange"))
        authority_urn = c.get("companyUrn") or c.get("*company")
        out.append(
            Certification(
                name=_text(c.get("name")),
                issuer=_text(c.get("authority")),
                issuer_logo=_company_logo(index, authority_urn),
                issue_date=dates.start.to_iso() if dates and dates.start else None,
                expiration_date=dates.end.to_iso() if dates and dates.end else None,
                credential_id=_text(c.get("licenseNumber")),
                credential_url=_text(c.get("url")),
            )
        )
    return out


def _languages(index: EntityIndex) -> list[Language]:
    out: list[Language] = []
    for lang in index.by_type("profile.Language", "Language"):
        name = _text(lang.get("name"))
        if name:
            out.append(Language(name=name, proficiency=_text(lang.get("proficiency"))))
    return out


def _projects(index: EntityIndex) -> list[Project]:
    return [
        Project(
            name=_text(p.get("title")) or _text(p.get("name")),
            description=_text(p.get("description")),
            url=_text(p.get("url")),
            dates=parse_structured_range(p.get("dateRange")),
        )
        for p in index.by_type("profile.Project", "Project")
        if _text(p.get("title")) or _text(p.get("name"))
    ]


def _publications(index: EntityIndex) -> list[Publication]:
    return [
        Publication(
            name=_text(p.get("name")),
            publisher=_text(p.get("publisher")),
            date=_iso(p.get("publishedOn") or p.get("date")),
            description=_text(p.get("description")),
            url=_text(p.get("url")),
        )
        for p in index.by_type("profile.Publication", "Publication")
        if _text(p.get("name"))
    ]


def _honors(index: EntityIndex) -> list[Honor]:
    return [
        Honor(
            title=_text(h.get("title")),
            issuer=_text(h.get("issuer")),
            date=_iso(h.get("issuedOn") or h.get("issueDate")),
            description=_text(h.get("description")),
        )
        for h in index.by_type("profile.Honor", "Honor")
        if _text(h.get("title"))
    ]


def _volunteering(index: EntityIndex) -> list[Volunteering]:
    return [
        Volunteering(
            role=_text(v.get("role")),
            organization=_text(v.get("companyName")),
            cause=_text(v.get("cause")),
            dates=parse_structured_range(v.get("dateRange")),
            description=_text(v.get("description")),
        )
        for v in index.by_type("VolunteerExperience")
        if _text(v.get("role")) or _text(v.get("companyName"))
    ]


def _courses(index: EntityIndex) -> list[Course]:
    return [
        Course(name=_text(c.get("name")), number=_text(c.get("number")))
        for c in index.by_type("profile.Course", "Course")
        if _text(c.get("name"))
    ]


def _patents(index: EntityIndex) -> list[Patent]:
    return [
        Patent(
            title=_text(p.get("title")),
            number=_text(p.get("number") or p.get("applicationNumber")),
            date=_iso(p.get("issuedOn") or p.get("filingDate")),
            description=_text(p.get("description")),
            url=_text(p.get("url")),
        )
        for p in index.by_type("profile.Patent", "Patent")
        if _text(p.get("title"))
    ]


def _organizations(index: EntityIndex) -> list[Organization]:
    return [
        Organization(
            name=_text(o.get("name")),
            role=_text(o.get("position")),
            dates=parse_structured_range(o.get("dateRange")),
            description=_text(o.get("description")),
        )
        for o in index.by_type("profile.Organization", "Organization")
        if _text(o.get("name"))
    ]


def _test_scores(index: EntityIndex) -> list[TestScore]:
    return [
        TestScore(
            name=_text(t.get("name")),
            score=_text(t.get("score")),
            date=_iso(t.get("dateOn") or t.get("date")),
            description=_text(t.get("description")),
        )
        for t in index.by_type("profile.TestScore", "TestScore")
        if _text(t.get("name"))
    ]


# -------------------------------------------------------------------- helpers


def _text(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        # Multi-locale wrappers such as {"en_US": "Bill"}.
        for key in ("en_US", "text", "localized"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _iso(raw: object) -> str | None:
    from app.parsing.dates import parse_partial_date

    parsed = parse_partial_date(raw)
    return parsed.to_iso() if parsed else None
