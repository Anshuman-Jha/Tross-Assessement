"""Structured logging with hard credential redaction.

The redaction processor is the important part: this service handles session
cookies on every request, and a cookie leaked into a log aggregator is a
compromised LinkedIn account. Redaction runs as a structlog processor so it
applies to every event regardless of the call site remembering to scrub.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

#: Keys whose values are replaced wholesale.
SENSITIVE_KEYS = frozenset(
    {
        "li_at",
        "jsessionid",
        "cookie",
        "cookies",
        "set-cookie",
        "csrf-token",
        "csrf_token",
        "authorization",
        "password",
        "session_password",
        "session_key",
        "api_key",
        "x-api-key",
        "linkedin_password",
        "linkedin_li_at",
        "linkedin_jsessionid",
    }
)

#: Catches cookie material embedded in free-text strings (URLs, error bodies).
_INLINE_PATTERNS = [
    re.compile(r"(li_at=)[^;\s\"']+", re.IGNORECASE),
    re.compile(r"(JSESSIONID=\"?)[^;\s\"']+", re.IGNORECASE),
    re.compile(r"(csrf-token:\s*)\S+", re.IGNORECASE),
]

REDACTED = "***REDACTED***"


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            k: (REDACTED if str(k).lower() in SENSITIVE_KEYS else _scrub(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, depth + 1) for v in value)
    if isinstance(value, str):
        for pattern in _INLINE_PATTERNS:
            value = pattern.sub(rf"\1{REDACTED}", value)
        return value
    return value


def redact_processor(
    _logger: object, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    return _scrub(event_dict)


def request_id_processor(
    _logger: object, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    rid = request_id_var.get()
    if rid:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    # uvicorn duplicates access logs we already emit via middleware.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        request_id_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
