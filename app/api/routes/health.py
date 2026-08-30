"""Health, readiness and metrics.

The split matters for deployment: ``/healthz`` answers "is the process alive"
(so the platform does not restart-loop a container whose LinkedIn cookie merely
expired), while ``/readyz`` answers "can this instance actually serve traffic".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings

router = APIRouter(tags=["ops"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Always 200 while the process is running."""
    settings = get_settings()
    return {"status": "ok", "service": settings.app_name, "environment": settings.environment}


@router.get("/readyz", summary="Readiness probe")
async def readyz(request: Request) -> JSONResponse:
    """503 unless a usable LinkedIn session and a built service exist.

    Deliberately reports session *health* without ever exposing cookie values.
    """
    state = request.app.state
    pool = getattr(state, "session_pool", None)
    service = getattr(state, "profile_service", None)
    resolver = getattr(state, "query_ids", None)
    cache = getattr(state, "cache", None)

    healthy_sessions = pool.healthy_count() if pool else 0
    ready = bool(service) and healthy_sessions > 0

    body: dict[str, Any] = {
        "ready": ready,
        "sessions": pool.snapshot() if pool else {"total": 0, "healthy": 0},
        "query_ids": resolver.snapshot() if resolver else None,
        "cache": cache.backend if cache else None,
    }
    if not ready:
        body["reason"] = (
            "No healthy LinkedIn session. Check that LINKEDIN_LI_AT is set and "
            "that the cookie has not expired."
            if healthy_sessions == 0
            else "Service failed to initialise."
        )
    return JSONResponse(status_code=200 if ready else 503, content=body)


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics() -> Response:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
