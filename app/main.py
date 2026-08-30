"""FastAPI application factory and process lifecycle.

Startup deliberately does **not** require a working LinkedIn session. A bad or
expired cookie makes the service *unready* (`/readyz` → 503) rather than
crashing it, so the container starts, reports why it cannot serve, and recovers
on its own once a valid cookie is supplied — instead of restart-looping.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import configure_limiter
from app.api.errors import register_error_handlers
from app.api.routes import health, profile
from app.config import Settings, get_settings
from app.linkedin.client import VoyagerClient
from app.linkedin.fetcher import ProfileFetcher
from app.linkedin.query_ids import QueryIdResolver
from app.linkedin.session_pool import build_pool_from_settings
from app.observability import metrics
from app.observability.logging import configure_logging, get_logger, request_id_var
from app.services.cache import build_cache
from app.services.profile_service import ProfileService

logger = get_logger(__name__)

DESCRIPTION = """
A reverse-engineered LinkedIn profile API.

Give it a LinkedIn profile URL, get structured JSON: name, headline, location,
about, experience, education, skills, certifications, languages, images and
more.

**How it works.** Requests go directly to LinkedIn's internal *Voyager*
endpoints over plain HTTPS. There is no browser, no headless Chrome and no
JavaScript execution anywhere in the stack.

**Three acquisition tiers**, tried in order and reported in `meta.source`:

1. `voyager_graphql` — the modern GraphQL profile-card endpoints.
2. `voyager_rest` — the legacy `profileView` endpoint. Structured dates, so
   higher fidelity where it still answers.
3. `embedded_html` — Voyager payloads inlined in the server-rendered page.
   Needs no `queryId`, so it survives LinkedIn rotating them.

**Authentication.** Send your key in the `X-API-Key` header.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    configure_limiter(max_requests=60, window_seconds=60.0)

    logger.info(
        "app.starting",
        environment=settings.environment,
        accounts=len(settings.accounts),
        graphql=settings.enable_graphql_tier,
        rest=settings.enable_rest_tier,
        html=settings.enable_html_tier,
    )
    if not settings.has_credentials:
        # Not fatal: the service starts and reports itself unready, which is
        # far easier to diagnose than a crash loop.
        logger.error("app.no_credentials_configured")

    pool = build_pool_from_settings(
        settings.accounts,
        requests_per_minute=settings.requests_per_minute_per_session,
        cooldown_seconds=settings.session_cooldown_seconds,
    )
    client = VoyagerClient(
        pool,
        timeout=settings.request_timeout_seconds,
        max_retries=settings.max_retries,
        max_concurrent=settings.max_concurrent_upstream,
        min_delay_ms=settings.min_delay_between_requests_ms,
        jitter_ms=settings.jitter_ms,
        cooldown_seconds=settings.session_cooldown_seconds,
    )
    resolver = QueryIdResolver(
        path=Path(settings.query_id_file) if settings.query_id_file else None,
        ttl_seconds=settings.query_id_discovery_ttl_seconds,
        discovery_enabled=settings.enable_query_id_discovery,
    )
    fetcher = ProfileFetcher(
        client,
        resolver,
        enable_graphql=settings.enable_graphql_tier,
        enable_rest=settings.enable_rest_tier,
        enable_html=settings.enable_html_tier,
    )
    cache = build_cache(settings.redis_url, settings.cache_max_entries)

    app.state.settings = settings
    app.state.session_pool = pool
    app.state.voyager_client = client
    app.state.query_ids = resolver
    app.state.cache = cache
    app.state.profile_service = ProfileService(fetcher, cache, ttl=settings.cache_ttl_seconds)

    metrics.HEALTHY_SESSIONS.set(pool.healthy_count())
    logger.info("app.started", sessions=len(pool), cache=cache.backend)

    try:
        yield
    finally:
        logger.info("app.shutting_down")
        await client.aclose()
        await cache.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="LinkedIn Profile API",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Source", "url": "https://github.com/"},
        license_info={"name": "MIT"},
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

    @app.middleware("http")
    async def observability_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.monotonic()
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)

        try:
            response = await call_next(request)
        except Exception:
            # The handler chain renders the body; record the failure and re-raise.
            metrics.REQUESTS.labels(request.method, path, "500").inc()
            raise
        finally:
            duration = time.monotonic() - started
            metrics.REQUEST_DURATION.labels(path).observe(duration)
            request_id_var.reset(token)

        metrics.REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = str(round(duration * 1000))
        return response

    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(profile.router)

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse(
            {
                "service": "LinkedIn Profile API",
                "version": "1.0.0",
                "docs": "/docs",
                "health": "/healthz",
                "readiness": "/readyz",
                "endpoints": {
                    "POST /api/v1/profile": 'body: {"url": "<linkedin profile url>"}',
                    "GET /api/v1/profile?url=...": "query-string form",
                    "GET /api/v1/profile/{public_id}": "by public identifier",
                },
            }
        )

    return app


app = create_app()
