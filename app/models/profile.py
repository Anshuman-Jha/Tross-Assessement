"""The public response schema.

The challenge leaves the schema to us, so it is designed around three rules:

1. **Stable keys.** Every section is always present. A profile with no
   certifications returns ``"certifications": []``, never a missing key, so
   consumers never need defensive lookups.
2. **Honest nulls.** A field is ``null`` when LinkedIn did not give it to us.
   Nothing is invented, defaulted, or inferred.
3. **Provenance in the envelope.** ``meta.source`` names which acquisition tier
   answered and ``meta.completeness`` flags which sections were actually
   populated, so a partial result is visibly partial rather than silently thin.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.parsing.dates import DateRange
from app.parsing.images import Image


class Source(StrEnum):
    """Which acquisition tier produced the payload."""

    #: The modern Rest.li finder. Needs no queryId, returns typed fields.
    DASH = "voyager_dash"
    GRAPHQL = "voyager_graphql"
    REST = "voyager_rest"
    HTML = "embedded_html"
    CACHE = "cache"


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# ------------------------------------------------------------------- sections


class Location(_Base):
    full: str | None = None
    city: str | None = None
    country: str | None = None
    country_code: str | None = None


class ContactInfo(_Base):
    websites: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    twitter: list[str] = Field(default_factory=list)
    birthday: str | None = None
    address: str | None = None


class Basics(_Base):
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    headline: str | None = None
    about: str | None = Field(default=None, description="The 'About' summary section.")
    pronouns: str | None = None
    location: Location = Field(default_factory=Location)
    industry: str | None = None
    public_identifier: str | None = None
    profile_url: str | None = None
    connections: int | None = Field(
        default=None, description="LinkedIn caps the displayed count at 500."
    )
    followers: int | None = None
    is_premium: bool | None = None
    is_influencer: bool | None = None
    is_open_to_work: bool | None = None
    is_hiring: bool | None = None
    profile_picture: Image | None = None
    background_image: Image | None = None
    contact: ContactInfo = Field(default_factory=ContactInfo)


class Experience(_Base):
    title: str | None = None
    company: str | None = None
    company_url: str | None = None
    company_urn: str | None = None
    company_logo: Image | None = None
    employment_type: str | None = Field(
        default=None, description="Full-time, Contract, Internship, ..."
    )
    location: str | None = None
    location_type: str | None = Field(default=None, description="Remote, Hybrid, On-site.")
    dates: DateRange | None = None
    is_current: bool = False
    description: str | None = None
    skills: list[str] = Field(default_factory=list)


class Education(_Base):
    school: str | None = None
    school_url: str | None = None
    school_urn: str | None = None
    school_logo: Image | None = None
    degree: str | None = None
    field_of_study: str | None = None
    grade: str | None = None
    dates: DateRange | None = None
    description: str | None = None
    activities: str | None = None


class Skill(_Base):
    name: str
    endorsement_count: int | None = None
    insights: list[str] = Field(default_factory=list)


class Certification(_Base):
    name: str | None = None
    issuer: str | None = None
    issuer_url: str | None = None
    issuer_logo: Image | None = None
    issue_date: str | None = None
    expiration_date: str | None = None
    credential_id: str | None = None
    credential_url: str | None = None


class Language(_Base):
    name: str
    proficiency: str | None = None


class Project(_Base):
    name: str | None = None
    description: str | None = None
    url: str | None = None
    dates: DateRange | None = None


class Publication(_Base):
    name: str | None = None
    publisher: str | None = None
    date: str | None = None
    description: str | None = None
    url: str | None = None


class Honor(_Base):
    title: str | None = None
    issuer: str | None = None
    date: str | None = None
    description: str | None = None


class Volunteering(_Base):
    role: str | None = None
    organization: str | None = None
    cause: str | None = None
    dates: DateRange | None = None
    description: str | None = None


class Course(_Base):
    name: str | None = None
    number: str | None = None


class Patent(_Base):
    title: str | None = None
    number: str | None = None
    date: str | None = None
    description: str | None = None
    url: str | None = None


class Organization(_Base):
    name: str | None = None
    role: str | None = None
    dates: DateRange | None = None
    description: str | None = None


class TestScore(_Base):
    name: str | None = None
    score: str | None = None
    date: str | None = None
    description: str | None = None


class Recommendation(_Base):
    author_name: str | None = None
    author_headline: str | None = None
    author_profile_url: str | None = None
    relationship: str | None = None
    text: str | None = None


class Recommendations(_Base):
    received: list[Recommendation] = Field(default_factory=list)
    given: list[Recommendation] = Field(default_factory=list)


class Profile(_Base):
    basics: Basics = Field(default_factory=Basics)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    honors: list[Honor] = Field(default_factory=list)
    volunteering: list[Volunteering] = Field(default_factory=list)
    courses: list[Course] = Field(default_factory=list)
    patents: list[Patent] = Field(default_factory=list)
    organizations: list[Organization] = Field(default_factory=list)
    test_scores: list[TestScore] = Field(default_factory=list)
    recommendations: Recommendations = Field(default_factory=Recommendations)


# ------------------------------------------------------------------- envelope


class ResponseMeta(_Base):
    profile_url: str
    public_identifier: str
    profile_urn: str | None = None
    fetched_at: datetime
    source: Source
    cached: bool = False
    duration_ms: int = 0
    completeness: dict[str, bool] = Field(
        default_factory=dict,
        description="Per-section flag: true when that section returned data.",
    )


class ProfileResponse(_Base):
    success: bool = True
    meta: ResponseMeta
    profile: Profile
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal problems, e.g. a section that failed to load.",
    )


class ErrorResponse(_Base):
    """RFC 7807-shaped error body used for every failure."""

    success: bool = False
    error: str = Field(description="Stable machine-readable code, e.g. PROFILE_NOT_FOUND.")
    message: str = Field(description="Human-readable explanation.")
    detail: dict[str, object] = Field(default_factory=dict)
    request_id: str | None = None


class ProfileRequest(_Base):
    url: str = Field(
        description="A LinkedIn profile URL or bare public identifier.",
        examples=["https://www.linkedin.com/in/williamhgates/", "williamhgates"],
        min_length=1,
        max_length=500,
    )
