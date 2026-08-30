"""Section mappers: turning flattened entities into the public schema."""

from __future__ import annotations

from app.parsing.components import flatten_card_payload
from app.parsing.sections.education import parse_education
from app.parsing.sections.experience import parse_experience
from app.parsing.sections.simple import (
    parse_certifications,
    parse_languages,
    parse_skills,
)
from tests.factories import card, entity_component, vector_image


def _experience(*entities: dict) -> list:
    return parse_experience(flatten_card_payload(card("experience", list(entities))))


# ----------------------------------------------------------------- experience


def test_single_role_splits_company_and_employment_type() -> None:
    (job,) = _experience(
        entity_component(
            title="Senior Software Engineer",
            subtitle="Acme Corp · Full-time",
            caption="Mar 2021 - Present · 3 yrs 2 mos",
            metadata="London, United Kingdom · Hybrid",
            image=vector_image(),
            link="https://www.linkedin.com/company/acme/",
            descriptions=["Led the payments rewrite."],
        )
    )

    assert job.title == "Senior Software Engineer"
    assert job.company == "Acme Corp"
    assert job.employment_type == "Full-time"
    assert job.location == "London, United Kingdom"
    assert job.location_type == "Hybrid"
    assert job.is_current is True
    assert job.dates is not None and job.dates.start is not None
    assert (job.dates.start.year, job.dates.start.month) == (2021, 3)
    assert job.description == "Led the payments rewrite."
    assert job.company_logo is not None


def test_grouped_employer_expands_into_one_entry_per_role() -> None:
    """The promotion history must survive, with the company carried down.

    Naively flattening this shape yields a single fake job titled "Acme Corp"
    and silently discards both real roles.
    """
    jobs = _experience(
        entity_component(
            title="Acme Corp",
            caption="4 yrs 1 mo",
            link="https://www.linkedin.com/company/acme/",
            image=vector_image(),
            children=[
                entity_component(
                    title="Senior Engineer",
                    subtitle="Full-time",
                    caption="Mar 2023 - Present · 1 yr",
                    metadata="London · Remote",
                ),
                entity_component(
                    title="Engineer",
                    subtitle="Full-time",
                    caption="Mar 2021 - Mar 2023 · 2 yrs",
                ),
            ],
        )
    )

    assert [j.title for j in jobs] == ["Senior Engineer", "Engineer"]
    assert all(j.company == "Acme Corp" for j in jobs)
    assert all(j.company_logo is not None for j in jobs), "logo inherits from the employer"
    assert jobs[0].is_current is True
    assert jobs[0].location_type == "Remote"
    assert jobs[1].is_current is False
    assert jobs[1].dates is not None and jobs[1].dates.duration_months == 25


def test_company_with_no_employment_type() -> None:
    (job,) = _experience(entity_component(title="Founder", subtitle="Acme Corp"))
    assert job.company == "Acme Corp"
    assert job.employment_type is None


# ------------------------------------------------------------------ education


def test_education_splits_degree_from_field() -> None:
    (edu,) = parse_education(
        flatten_card_payload(
            card(
                "education",
                [
                    entity_component(
                        title="Massachusetts Institute of Technology",
                        subtitle="Bachelor of Science - BS, Computer Science",
                        caption="2015 - 2019",
                        descriptions=[
                            "Grade: 3.9",
                            "Activities and societies: Robotics Club",
                            "Focused on distributed systems.",
                        ],
                    )
                ],
            )
        )
    )

    assert edu.school == "Massachusetts Institute of Technology"
    assert edu.degree == "Bachelor of Science - BS"
    assert edu.field_of_study == "Computer Science"
    assert edu.grade == "3.9"
    assert edu.activities == "Robotics Club"
    assert edu.description == "Focused on distributed systems."
    assert edu.dates is not None and edu.dates.start is not None
    assert edu.dates.start.year == 2015


def test_degree_containing_a_comma_splits_on_the_last_one() -> None:
    (edu,) = parse_education(
        flatten_card_payload(
            card(
                "education",
                [entity_component(title="Oxford", subtitle="BSc, Honours, Physics")],
            )
        )
    )
    assert edu.degree == "BSc, Honours"
    assert edu.field_of_study == "Physics"


# --------------------------------------------------------------------- skills


def test_skills_deduplicate_and_read_endorsements() -> None:
    skills = parse_skills(
        flatten_card_payload(
            card(
                "skills",
                [
                    entity_component(title="Python", descriptions=["42 endorsements"]),
                    entity_component(title="python"),
                    entity_component(title="Go"),
                ],
            )
        )
    )
    assert [s.name for s in skills] == ["Python", "Go"]
    assert skills[0].endorsement_count == 42
    assert skills[1].endorsement_count is None


# ------------------------------------------------------------- certifications


def test_certification_parses_issue_expiry_and_credential() -> None:
    (cert,) = parse_certifications(
        flatten_card_payload(
            card(
                "certifications",
                [
                    entity_component(
                        title="AWS Certified Solutions Architect",
                        subtitle="Amazon Web Services",
                        caption="Issued Jan 2023 · Expires Jan 2026",
                        metadata="Credential ID ABC-123",
                        link="https://aws.amazon.com/verify/ABC-123",
                    )
                ],
            )
        )
    )

    assert cert.name == "AWS Certified Solutions Architect"
    assert cert.issuer == "Amazon Web Services"
    assert cert.issue_date == "Jan 2023"
    assert cert.expiration_date == "Jan 2026"
    assert cert.credential_id == "ABC-123"
    assert cert.credential_url == "https://aws.amazon.com/verify/ABC-123"


def test_certification_with_a_bare_date_caption() -> None:
    (cert,) = parse_certifications(
        flatten_card_payload(
            card("certifications", [entity_component(title="CKA", caption="Mar 2024")])
        )
    )
    assert cert.issue_date == "Mar 2024"
    assert cert.expiration_date is None


# ------------------------------------------------------------------ languages


def test_languages_read_proficiency() -> None:
    langs = parse_languages(
        flatten_card_payload(
            card(
                "languages",
                [
                    entity_component(title="English", caption="Native or bilingual proficiency"),
                    entity_component(title="French", subtitle="Limited working proficiency"),
                ],
            )
        )
    )
    assert langs[0].name == "English"
    assert langs[0].proficiency == "Native or bilingual proficiency"
    assert langs[1].proficiency == "Limited working proficiency"


def test_empty_sections_return_empty_lists() -> None:
    for parser in (parse_experience, parse_education, parse_skills, parse_languages):
        assert parser([]) == []
