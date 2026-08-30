"""Prometheus metrics.

Scoped to the questions that actually get asked when this service misbehaves:
which tier is answering, how often LinkedIn throttles us, and is the cache
carrying its weight.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "linkedin_api_requests_total",
    "API requests received.",
    ["method", "path", "status"],
)

REQUEST_DURATION = Histogram(
    "linkedin_api_request_duration_seconds",
    "End-to-end API request latency.",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60),
)

PROFILE_FETCHES = Counter(
    "linkedin_profile_fetches_total",
    "Profile fetches by the tier that answered.",
    ["source", "outcome"],
)

TIER_FAILURES = Counter(
    "linkedin_tier_failures_total",
    "Acquisition tier failures by error type.",
    ["tier", "error"],
)

CACHE_EVENTS = Counter(
    "linkedin_cache_events_total",
    "Cache hits and misses.",
    ["event"],
)

UPSTREAM_ERRORS = Counter(
    "linkedin_upstream_errors_total",
    "Errors returned by LinkedIn.",
    ["code"],
)

HEALTHY_SESSIONS = Gauge(
    "linkedin_healthy_sessions",
    "LinkedIn sessions currently able to serve requests.",
)
