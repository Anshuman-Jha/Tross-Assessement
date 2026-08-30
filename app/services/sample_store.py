"""Recorded sample responses, for demonstrating the API without a live session.

LinkedIn session cookies are short-lived, and a deployed demo whose credential
has expired can otherwise only show an error — which tells a visitor nothing
about whether the service works.

So the repository ships a **recorded response**: a real
``/voyager/api/identity/dash/profiles`` payload, captured live and then
sanitised, replayed through the ordinary parsers.

Two rules make this honest rather than a fake:

1. **The data is real.** It is a genuine LinkedIn response parsed by the same
   code path as a live request — not hand-written JSON shaped to look right.
2. **It is never passed off as live.** ``meta.source`` is ``recorded_sample``,
   ``meta.is_live`` is ``false``, and a warning states plainly that this is a
   recording and how to get live results. A caller cannot mistake one for the
   other, and nothing else in the service consults this store.

Samples are only served when there is no usable session *and* the request is
for a slug we have a recording of. A live session always wins.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from app.models.profile import Profile, ProfileResponse, ResponseMeta, Source
from app.observability.logging import get_logger
from app.parsing.dash_profile import parse_dash_profile

logger = get_logger(__name__)

SAMPLES_DIR = Path(__file__).parent.parent / "samples"

#: Slugs that map to a bundled recording. The keys are what a caller types.
SAMPLE_SLUGS: dict[str, str] = {
    "ada-lovelace": "dash_profile.json",
}

RECORDED_WARNING = (
    "This is a RECORDED sample, not a live fetch. It is a real LinkedIn API "
    "response captured earlier and replayed through the same parsers, served "
    "because no valid LinkedIn session is currently configured. Set "
    "LINKEDIN_LI_AT to fetch live profiles."
)


@lru_cache(maxsize=8)
def _load(filename: str) -> Profile | None:
    path = SAMPLES_DIR / filename
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - packaging error
        logger.warning("samples.unreadable", file=filename, error=str(exc))
        return None
    return parse_dash_profile(payload)


def has_sample(public_id: str) -> bool:
    return public_id.lower() in SAMPLE_SLUGS


def available_samples() -> list[str]:
    return sorted(SAMPLE_SLUGS)


def build_sample_response(public_id: str) -> ProfileResponse | None:
    """A recorded response, explicitly labelled as such."""
    filename = SAMPLE_SLUGS.get(public_id.lower())
    if not filename:
        return None
    profile = _load(filename)
    if profile is None:
        return None

    completeness = {
        "basics": bool(profile.basics.full_name or profile.basics.headline),
        "about": bool(profile.basics.about),
        "experience": bool(profile.experience),
        "education": bool(profile.education),
        "skills": bool(profile.skills),
        "certifications": bool(profile.certifications),
        "languages": bool(profile.languages),
        "profile_picture": profile.basics.profile_picture is not None,
    }

    logger.info("samples.served", public_id=public_id)
    return ProfileResponse(
        success=True,
        meta=ResponseMeta(
            profile_url=profile.basics.profile_url
            or f"https://www.linkedin.com/in/{public_id}",
            public_identifier=profile.basics.public_identifier or public_id,
            profile_urn=None,
            fetched_at=datetime.now(UTC),
            source=Source.RECORDED,
            is_live=False,
            cached=False,
            duration_ms=0,
            completeness=completeness,
        ),
        profile=profile,
        warnings=[RECORDED_WARNING],
    )
