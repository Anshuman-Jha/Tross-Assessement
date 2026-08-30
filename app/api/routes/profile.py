"""Profile endpoints."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import rate_limit
from app.models.flat import to_flat
from app.models.profile import ErrorResponse, ProfileRequest, ProfileResponse
from app.services.profile_service import ProfileService

router = APIRouter(prefix="/api/v1", tags=["profile"])


class ResponseFormat(StrEnum):
    """Which projection of the profile to return."""

    NESTED = "nested"
    FLAT = "flat"


_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    403: {"model": ErrorResponse, "description": "Profile is private or out of network."},
    404: {"model": ErrorResponse, "description": "No such LinkedIn profile."},
    422: {"model": ErrorResponse, "description": "The supplied URL is not a profile URL."},
    429: {"model": ErrorResponse, "description": "Rate limited (by this API or by LinkedIn)."},
    502: {"model": ErrorResponse, "description": "LinkedIn session invalid, or all tiers failed."},
    503: {"model": ErrorResponse, "description": "No healthy LinkedIn session available."},
}

FormatParam = Annotated[
    ResponseFormat,
    Query(
        description=(
            "`nested` (default) returns the full schema. `flat` returns a "
            "PhantomBuster-style single-level record for CSV/spreadsheet use."
        )
    ),
]
RefreshParam = Annotated[bool, Query(description="Bypass the cache and re-fetch.")]


def _service(request: Request) -> ProfileService:
    service: ProfileService | None = getattr(request.app.state, "profile_service", None)
    if service is None:  # pragma: no cover - only during a failed startup
        from app.linkedin.exceptions import NoHealthySessionError

        raise NoHealthySessionError("The service is still starting up.")
    return service


def _render(response: ProfileResponse, fmt: ResponseFormat) -> Any:
    """Return the nested model, or its flat projection."""
    if fmt is ResponseFormat.FLAT:
        return JSONResponse(content=to_flat(response))
    return response


@router.post(
    "/profile",
    response_model=None,
    responses={200: {"model": ProfileResponse}, **_RESPONSES},
    summary="Fetch a LinkedIn profile by URL",
    dependencies=[Depends(rate_limit)],
)
async def post_profile(
    payload: ProfileRequest,
    request: Request,
    format: FormatParam = ResponseFormat.NESTED,
) -> Any:
    """Retrieve a LinkedIn profile as structured JSON.

    Accepts any profile URL shape (`www.`, regional subdomains, tracking query
    strings) or a bare public identifier.
    """
    result = await _service(request).get_profile(payload.url)
    return _render(result, format)


@router.get(
    "/profile",
    response_model=None,
    responses={200: {"model": ProfileResponse}, **_RESPONSES},
    summary="Fetch a LinkedIn profile by URL (query-string form)",
    dependencies=[Depends(rate_limit)],
)
async def get_profile(
    request: Request,
    url: Annotated[
        str,
        Query(
            description="LinkedIn profile URL or public identifier.",
            examples=["https://www.linkedin.com/in/williamhgates/"],
            min_length=1,
            max_length=500,
        ),
    ],
    refresh: RefreshParam = False,
    format: FormatParam = ResponseFormat.NESTED,
) -> Any:
    """Convenience form of `POST /api/v1/profile`, easy to hit with curl."""
    result = await _service(request).get_profile(url, refresh=refresh)
    return _render(result, format)


@router.get(
    "/profile/{public_id}",
    response_model=None,
    responses={200: {"model": ProfileResponse}, **_RESPONSES},
    summary="Fetch a LinkedIn profile by public identifier",
    dependencies=[Depends(rate_limit)],
)
async def get_profile_by_id(
    public_id: str,
    request: Request,
    refresh: RefreshParam = False,
    format: FormatParam = ResponseFormat.NESTED,
) -> Any:
    """Fetch by the slug alone, e.g. `/api/v1/profile/williamhgates`."""
    result = await _service(request).get_profile(public_id, refresh=refresh)
    return _render(result, format)
