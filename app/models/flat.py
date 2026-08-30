"""PhantomBuster-compatible flat projection.

The challenge cites PhantomBuster's *LinkedIn Profile Scraper* as the reference
product. That tool emits a **flat, spreadsheet-shaped record** — one row per
profile, with repeated sections unrolled into numbered columns
(``jobTitle``/``jobTitle2``, ``school``/``school2``, ``skill1``…``skill6``) —
because its output is consumed as CSV.

That shape is genuinely useful for the same reasons it is for them: it drops
straight into a sheet, a CRM import, or a dataframe. It is also lossy — nested
dates, image variants and per-role descriptions cannot survive flattening.

So both are offered rather than picking one:

* ``format=nested`` (default) — the full schema, nothing discarded.
* ``format=flat`` — this projection, for CSV-shaped consumers.

The flat form is a *view* over the nested model, derived at response time.
Parsers only ever produce the nested model, so the two cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from app.models.profile import DateRange, ProfileResponse

#: How many repeated entries get their own numbered columns, matching the
#: cap PhantomBuster applies for the same reason: unbounded columns break
#: spreadsheet consumers.
MAX_JOBS = 5
MAX_SCHOOLS = 3
MAX_SKILLS = 6
MAX_CERTS = 3


def _date_range_text(dates: DateRange | None) -> str | None:
    """Render a date range the way a profile page displays it."""
    if dates is None:
        return None
    if dates.duration:
        start = dates.start.to_iso() if dates.start else ""
        end = "Present" if dates.is_current else (dates.end.to_iso() if dates.end else "")
        return f"{start} - {end}".strip(" -") or dates.duration
    start = dates.start.to_iso() if dates.start else ""
    end = "Present" if dates.is_current else (dates.end.to_iso() if dates.end else "")
    rendered = f"{start} - {end}".strip()
    return rendered.strip(" -") or None


def to_flat(response: ProfileResponse) -> dict[str, Any]:
    """Project a :class:`ProfileResponse` into flat, CSV-friendly keys."""
    profile = response.profile
    basics = profile.basics
    meta = response.meta

    flat: dict[str, Any] = {
        # ---- identity
        "profileUrl": meta.profile_url,
        "linkedInProfileUrl": meta.profile_url,
        "publicIdentifier": meta.public_identifier,
        "vmid": meta.profile_urn.split(":")[-1] if meta.profile_urn else None,
        "firstName": basics.first_name,
        "lastName": basics.last_name,
        "fullName": basics.full_name,
        "headline": basics.headline,
        "description": basics.about,
        "location": basics.location.full,
        "city": basics.location.city,
        "country": basics.location.country,
        "industry": basics.industry,
        "pronouns": basics.pronouns,
        # ---- images
        "profileImageUrl": basics.profile_picture.url if basics.profile_picture else None,
        "backgroundImageUrl": basics.background_image.url if basics.background_image else None,
        # ---- network
        "connectionsCount": basics.connections,
        "followersCount": basics.followers,
        "isPremium": basics.is_premium,
        "isInfluencer": basics.is_influencer,
        "isOpenToWork": basics.is_open_to_work,
        # ---- contact
        "mail": basics.contact.emails[0] if basics.contact.emails else None,
        "phoneNumber": basics.contact.phones[0] if basics.contact.phones else None,
        "twitter": basics.contact.twitter[0] if basics.contact.twitter else None,
        "website": basics.contact.websites[0] if basics.contact.websites else None,
        # ---- provenance, kept so a flat row is still traceable
        "timestamp": meta.fetched_at.isoformat(),
        "source": meta.source.value,
        "cached": meta.cached,
    }

    # ---- current position, promoted to unsuffixed keys ---------------------
    current = next((j for j in profile.experience if j.is_current), None)
    if current is None and profile.experience:
        current = profile.experience[0]
    if current is not None:
        flat.update(
            {
                "company": current.company,
                "companyName": current.company,
                "companyUrl": current.company_url,
                "jobTitle": current.title,
                "jobDateRange": _date_range_text(current.dates),
                "jobLocation": current.location,
                "jobDescription": current.description,
            }
        )

    # ---- experience, unrolled ----------------------------------------------
    for i, job in enumerate(profile.experience[:MAX_JOBS], start=1):
        suffix = "" if i == 1 else str(i)
        flat[f"company{suffix or '1'}"] = job.company
        flat[f"jobTitle{suffix or '1'}"] = job.title
        flat[f"jobDateRange{suffix or '1'}"] = _date_range_text(job.dates)
        flat[f"jobLocation{suffix or '1'}"] = job.location
        flat[f"jobDescription{suffix or '1'}"] = job.description

    # ---- education, unrolled -----------------------------------------------
    for i, edu in enumerate(profile.education[:MAX_SCHOOLS], start=1):
        suffix = str(i)
        flat[f"school{suffix}"] = edu.school
        flat[f"schoolDegree{suffix}"] = edu.degree
        flat[f"schoolFieldOfStudy{suffix}"] = edu.field_of_study
        flat[f"schoolDateRange{suffix}"] = _date_range_text(edu.dates)
    if profile.education:
        first = profile.education[0]
        flat["school"] = first.school
        flat["schoolDegree"] = first.degree
        flat["schoolDateRange"] = _date_range_text(first.dates)

    # ---- skills --------------------------------------------------------------
    for i, skill in enumerate(profile.skills[:MAX_SKILLS], start=1):
        flat[f"skill{i}"] = skill.name
    flat["allSkills"] = ", ".join(s.name for s in profile.skills) or None
    flat["skillsCount"] = len(profile.skills)

    # ---- certifications ------------------------------------------------------
    for i, cert in enumerate(profile.certifications[:MAX_CERTS], start=1):
        flat[f"certification{i}"] = cert.name
        flat[f"certification{i}Issuer"] = cert.issuer
    flat["allCertifications"] = (
        ", ".join(c.name for c in profile.certifications if c.name) or None
    )

    # ---- languages -----------------------------------------------------------
    flat["allLanguages"] = ", ".join(lang.name for lang in profile.languages) or None

    # ---- counts, so a consumer can tell "absent" from "not scraped" ---------
    flat["experienceCount"] = len(profile.experience)
    flat["educationCount"] = len(profile.education)

    return flat
