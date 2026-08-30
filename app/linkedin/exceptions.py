"""Typed errors for the LinkedIn acquisition layer.

Each maps to a stable machine-readable `code` and an HTTP status, so callers can
branch on failure reasons without string-matching. The distinction that matters
most operationally is *retryable* vs *session-fatal*: retrying a 401 just burns
the account faster, so those are handled by cooling the session down instead.
"""

from __future__ import annotations


class LinkedInError(Exception):
    """Base for every upstream failure."""

    code: str = "LINKEDIN_ERROR"
    status_code: int = 502
    retryable: bool = False
    #: When true, the session that produced this error is quarantined.
    burns_session: bool = False

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class ProfileNotFoundError(LinkedInError):
    code = "PROFILE_NOT_FOUND"
    status_code = 404


class ProfilePrivateError(LinkedInError):
    """Profile exists but the authenticated account cannot see it."""

    code = "PROFILE_PRIVATE"
    status_code = 403


class InvalidProfileUrlError(LinkedInError):
    code = "INVALID_PROFILE_URL"
    status_code = 422


class AuthenticationError(LinkedInError):
    """The cookie is expired, revoked, or was never valid."""

    code = "SESSION_INVALID"
    status_code = 502
    burns_session = True


class ChallengeError(AuthenticationError):
    """LinkedIn demanded a CAPTCHA / 2FA / email checkpoint."""

    code = "LINKEDIN_CHALLENGE"
    status_code = 502
    burns_session = True


class RateLimitedError(LinkedInError):
    """HTTP 429, or LinkedIn's nonstandard 999."""

    code = "RATE_LIMITED"
    status_code = 429
    retryable = True
    burns_session = True

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class UpstreamError(LinkedInError):
    """5xx or transport failure talking to LinkedIn."""

    code = "UPSTREAM_ERROR"
    status_code = 503
    retryable = True


class QueryIdError(LinkedInError):
    """A GraphQL queryId was rejected — LinkedIn almost certainly rotated it.

    Signals the resolver to invalidate its cache and re-discover before retrying.
    """

    code = "QUERY_ID_INVALID"
    status_code = 503
    retryable = True


class NoHealthySessionError(LinkedInError):
    """Every configured session is quarantined or none were configured."""

    code = "NO_HEALTHY_SESSION"
    status_code = 503


class AllTiersFailedError(LinkedInError):
    """GraphQL, REST and HTML acquisition all failed for this profile."""

    code = "ALL_TIERS_FAILED"
    status_code = 502


class ParseError(LinkedInError):
    """A response arrived but did not have the shape we can read."""

    code = "PARSE_ERROR"
    status_code = 502
