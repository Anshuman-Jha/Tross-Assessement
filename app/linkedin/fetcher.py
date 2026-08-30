"""Orchestration: try each acquisition tier in turn, degrade rather than fail.

Two rules govern this module.

**Tiers fail independently.** GraphQL depends on a live queryId, REST on an
endpoint LinkedIn is retiring, HTML on neither. A failure mode that kills one
usually leaves the others standing, which is the entire point of having three.

**A partial profile beats an error.** If certifications 500 but experience and
education come back, the caller gets the profile plus a warning — never a 502.
Whatever did arrive is reported through ``meta.completeness``, so a thin result
is visibly thin instead of quietly passing for complete.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.linkedin import endpoints
from app.linkedin.client import VoyagerClient
from app.linkedin.exceptions import (
    AllTiersFailedError,
    AuthenticationError,
    LinkedInError,
    NoHealthySessionError,
    ProfileNotFoundError,
    ProfilePrivateError,
    QueryIdError,
)
from app.linkedin.html_fallback import (
    has_profile_content,
    looks_like_authwall,
    merge_payloads,
    payload_from_html,
)
from app.linkedin.query_ids import (
    PROFILE_BY_VANITY,
    PROFILE_CARDS,
    QueryIdResolver,
)
from app.models.profile import Profile, Source
from app.observability.logging import get_logger
from app.parsing.collection import EntityIndex, find_all
from app.parsing.components import FlatEntity, flatten_card_payload
from app.parsing.rest_profile import parse_rest_profile
from app.parsing.sections.basics import parse_basics, parse_contact_info
from app.parsing.sections.education import parse_education
from app.parsing.sections.experience import parse_experience
from app.parsing.sections.simple import (
    parse_certifications,
    parse_courses,
    parse_honors,
    parse_languages,
    parse_organizations,
    parse_patents,
    parse_projects,
    parse_publications,
    parse_skills,
    parse_test_scores,
    parse_volunteering,
)
from app.parsing.urn import normalize_profile_urn

logger = get_logger(__name__)

#: Maps a flattened section to the models it produces. Every section mapper has
#: this shape, which is what lets one loop drive all of them.
SectionParser = Callable[[list[FlatEntity]], list[Any]]

#: Sections fetched from the GraphQL card endpoint, mapped to their mapper.
_SECTION_PARSERS: dict[str, tuple[str, SectionParser]] = {
    "experience": ("experience", parse_experience),
    "education": ("education", parse_education),
    "skills": ("skills", parse_skills),
    "certifications": ("certifications", parse_certifications),
    "languages": ("languages", parse_languages),
    "projects": ("projects", parse_projects),
    "publications": ("publications", parse_publications),
    "honors": ("honors", parse_honors),
    "volunteering_experience": ("volunteering", parse_volunteering),
    "courses": ("courses", parse_courses),
    "patents": ("patents", parse_patents),
    "organizations": ("organizations", parse_organizations),
    "test_scores": ("test_scores", parse_test_scores),
}


def _merge_included(*payloads: object) -> dict[str, object]:
    """Combine several normalised envelopes into one entity graph.

    Delegates to the HTML tier's merger, which already implements URN-based
    deduplication for exactly this shape.
    """
    return merge_payloads([p for p in payloads if isinstance(p, dict)])


@dataclass
class FetchResult:
    profile: Profile
    source: Source
    profile_urn: str | None = None
    warnings: list[str] = field(default_factory=list)
    completeness: dict[str, bool] = field(default_factory=dict)

    def mark_completeness(self) -> None:
        p = self.profile
        self.completeness = {
            "basics": bool(p.basics.full_name or p.basics.headline),
            "about": bool(p.basics.about),
            "experience": bool(p.experience),
            "education": bool(p.education),
            "skills": bool(p.skills),
            "certifications": bool(p.certifications),
            "languages": bool(p.languages),
            "projects": bool(p.projects),
            "publications": bool(p.publications),
            "honors": bool(p.honors),
            "volunteering": bool(p.volunteering),
            "courses": bool(p.courses),
            "patents": bool(p.patents),
            "organizations": bool(p.organizations),
            "test_scores": bool(p.test_scores),
            "profile_picture": p.basics.profile_picture is not None,
        }

    @property
    def is_useful(self) -> bool:
        """Whether this result is worth returning rather than trying the next tier."""
        basics = self.profile.basics
        has_identity = bool(basics.full_name or basics.headline)
        has_content = any(
            getattr(self.profile, name) for name in ("experience", "education", "skills")
        )
        return has_identity or has_content


class ProfileFetcher:
    def __init__(
        self,
        client: VoyagerClient,
        query_ids: QueryIdResolver,
        *,
        enable_graphql: bool = True,
        enable_rest: bool = True,
        enable_html: bool = True,
        section_concurrency: int = 3,
    ) -> None:
        self._client = client
        self._query_ids = query_ids
        self._enable_graphql = enable_graphql
        self._enable_rest = enable_rest
        self._enable_html = enable_html
        self._section_semaphore = asyncio.Semaphore(section_concurrency)

    # ------------------------------------------------------------- entrypoint

    async def fetch(self, public_id: str) -> FetchResult:
        """Acquire a profile, trying each enabled tier in preference order."""
        attempts: list[tuple[str, Exception]] = []

        for name, enabled, runner in (
            ("graphql", self._enable_graphql, self._fetch_graphql),
            ("rest", self._enable_rest, self._fetch_rest),
            ("html", self._enable_html, self._fetch_html),
        ):
            if not enabled:
                continue
            started = time.monotonic()
            try:
                result = await runner(public_id)
            except (ProfileNotFoundError, ProfilePrivateError):
                # Definitive answers about the profile itself. No other tier
                # can do better, so stop rather than hammering LinkedIn twice more.
                raise
            except AuthenticationError:
                # The credential is bad; every tier uses the same one.
                raise
            except NoHealthySessionError:
                # No session exists to try. Every tier would report the same
                # thing, and "configure a cookie" is far more actionable than
                # a combined three-tier failure.
                raise
            except LinkedInError as exc:
                attempts.append((name, exc))
                logger.warning(
                    "fetcher.tier_failed", tier=name, error=type(exc).__name__, message=str(exc)
                )
                continue

            if result is not None and result.is_useful:
                result.mark_completeness()
                logger.info(
                    "fetcher.tier_succeeded",
                    tier=name,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    sections=[k for k, v in result.completeness.items() if v],
                )
                return result

            attempts.append((name, AllTiersFailedError(f"{name} tier returned no usable data")))
            logger.warning("fetcher.tier_empty", tier=name)

        raise AllTiersFailedError(
            "Could not retrieve this profile from LinkedIn. "
            + "; ".join(f"{n}: {e}" for n, e in attempts),
            detail={"attempts": {n: str(e) for n, e in attempts}},
        )

    # --------------------------------------------------------- tier 1: graphql

    async def _fetch_graphql(self, public_id: str) -> FetchResult | None:
        await self._query_ids.ensure_fresh(self._client)
        if not self._query_ids.is_usable:
            raise QueryIdError(
                "GraphQL query ids are unknown and discovery did not find them; "
                f"missing: {', '.join(self._query_ids.missing)}."
            )

        profile_urn, vanity_payload = await self._resolve_urn(public_id)
        if not profile_urn:
            raise ProfileNotFoundError(f"No LinkedIn profile found for {public_id!r}.")

        referer = endpoints.profile_html_url(public_id)
        warnings: list[str] = []

        top_card_payload = await self._fetch_card(profile_urn, "top_card", referer, warnings)
        profile = Profile()

        # The vanity lookup already returns the person's Profile entity, so its
        # payload is merged with the top card rather than discarded — that saves
        # a request and keeps names available even if the top card fails.
        merged = _merge_included(vanity_payload, top_card_payload)
        profile.basics = parse_basics(
            EntityIndex(merged),
            public_id=public_id,
            top_card=flatten_card_payload(top_card_payload) if top_card_payload else None,
        )

        # Fetch the list sections concurrently, each degrading independently.
        results = await asyncio.gather(
            *(
                self._fetch_and_parse_section(profile_urn, section, referer, warnings)
                for section in _SECTION_PARSERS
            )
        )
        for section, parsed in zip(_SECTION_PARSERS, results, strict=True):
            if parsed:
                attr, _ = _SECTION_PARSERS[section]
                setattr(profile, attr, parsed)

        if not profile.basics.about:
            profile.basics.about = await self._fetch_about(profile_urn, referer, warnings)

        return FetchResult(
            profile=profile,
            source=Source.GRAPHQL,
            profile_urn=profile_urn,
            warnings=warnings,
        )

    async def _resolve_urn(self, public_id: str) -> tuple[str | None, object]:
        """Vanity slug -> ``(urn:li:fsd_profile:..., the payload it came from)``.

        The payload is returned alongside because it already carries the
        person's Profile entity; the caller reuses it instead of refetching.
        """
        url = endpoints.profile_by_vanity_url(
            public_id, self._query_ids.get(PROFILE_BY_VANITY)
        )
        payload = await self._client.get_json(url, referer=endpoints.profile_html_url(public_id))
        index = EntityIndex(payload)

        for entity in index.entities:
            urn = entity.get("entityUrn")
            if isinstance(urn, str) and "fsd_profile" in urn:
                return normalize_profile_urn(urn), payload

        # Fall back to any profile URN anywhere in the payload.
        matches = find_all(
            payload,
            lambda d: isinstance(d.get("entityUrn"), str)
            and "fsd_profile" in d["entityUrn"],
        )
        if matches:
            return normalize_profile_urn(matches[0]["entityUrn"]), payload
        return None, payload

    async def _fetch_card(
        self, profile_urn: str, section: str, referer: str, warnings: list[str]
    ) -> object | None:
        url = endpoints.profile_cards_url(
            profile_urn, section, self._query_ids.get(PROFILE_CARDS)
        )
        async with self._section_semaphore:
            try:
                return await self._client.get_json(url, referer=referer)
            except QueryIdError:
                # Rotated mid-flight: invalidate so the next request re-discovers.
                self._query_ids.invalidate(PROFILE_CARDS)
                raise
            except LinkedInError as exc:
                warnings.append(f"Section {section!r} could not be loaded: {exc}")
                logger.warning("fetcher.section_failed", section=section, error=str(exc))
                return None

    async def _fetch_and_parse_section(
        self, profile_urn: str, section: str, referer: str, warnings: list[str]
    ) -> list[Any] | None:
        payload = await self._fetch_card(profile_urn, section, referer, warnings)
        if payload is None:
            return None
        _, parser = _SECTION_PARSERS[section]
        try:
            return parser(flatten_card_payload(payload))
        except Exception as exc:  # a parser bug must not sink the whole profile
            warnings.append(f"Section {section!r} could not be parsed: {exc}")
            logger.exception("fetcher.section_parse_failed", section=section)
            return None

    async def _fetch_about(
        self, profile_urn: str, referer: str, warnings: list[str]
    ) -> str | None:
        payload = await self._fetch_card(profile_urn, "about", referer, warnings)
        if payload is None:
            return None
        entities = flatten_card_payload(payload)
        for entity in entities:
            text = entity.description or entity.title
            if text:
                return text
        return None

    # ------------------------------------------------------------ tier 2: rest

    async def _fetch_rest(self, public_id: str) -> FetchResult | None:
        referer = endpoints.profile_html_url(public_id)
        payload = await self._client.get_json(
            endpoints.profile_view_url(public_id), referer=referer
        )
        profile = parse_rest_profile(payload, public_id=public_id)
        warnings: list[str] = []

        # Contact info lives behind its own endpoint and is optional.
        try:
            contact_payload = await self._client.get_json(
                endpoints.profile_contact_info_url(public_id), referer=referer
            )
            profile.basics.contact = parse_contact_info(contact_payload)
        except LinkedInError as exc:
            warnings.append(f"Contact info unavailable: {exc}")

        urn = None
        for entity in EntityIndex(payload).entities:
            candidate = entity.get("entityUrn")
            if isinstance(candidate, str) and "profile" in candidate.lower():
                urn = normalize_profile_urn(candidate)
                break

        return FetchResult(
            profile=profile, source=Source.REST, profile_urn=urn, warnings=warnings
        )

    # ------------------------------------------------------------ tier 3: html

    async def _fetch_html(self, public_id: str) -> FetchResult | None:
        url = endpoints.profile_html_url(public_id)
        page = await self._client.get_html(url, referer="https://www.linkedin.com/feed/")

        if looks_like_authwall(page):
            raise AuthenticationError(
                "LinkedIn served the public login wall instead of the profile. "
                "The li_at cookie is expired or invalid."
            )

        payload = payload_from_html(page)
        if not has_profile_content(payload):
            raise ProfileNotFoundError(
                f"No profile data was embedded in the page for {public_id!r}."
            )

        index = EntityIndex(payload)
        warnings = [
            "Served from embedded page data; long sections may be truncated to "
            "what LinkedIn renders on initial load."
        ]

        # The page embeds both shapes depending on which renderer served it, so
        # read it as REST first and top up from any component trees present.
        profile = parse_rest_profile(payload, public_id=public_id)
        entities = flatten_card_payload(payload)
        if entities:
            if not profile.experience:
                profile.experience = parse_experience(entities)
            if not profile.education:
                profile.education = parse_education(entities)
            if not profile.skills:
                profile.skills = parse_skills(entities)
        if not profile.basics.full_name:
            profile.basics = parse_basics(index, public_id=public_id, top_card=entities)

        urn = None
        for entity in index.entities:
            candidate = entity.get("entityUrn")
            if isinstance(candidate, str) and "fsd_profile" in candidate:
                urn = normalize_profile_urn(candidate)
                break

        return FetchResult(
            profile=profile, source=Source.HTML, profile_urn=urn, warnings=warnings
        )
