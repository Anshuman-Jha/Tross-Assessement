"""Exception handlers producing one consistent error envelope.

Every failure — validation, upstream, or unexpected — leaves through here, so
clients only ever have to parse one error shape. The stable ``error`` code is
the contract; ``message`` is for humans and may be reworded.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.linkedin.exceptions import LinkedInError
from app.models.profile import ErrorResponse
from app.observability.logging import get_logger, request_id_var

logger = get_logger(__name__)


def _render(
    status_code: int,
    code: str,
    message: str,
    detail: dict[str, object] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=code,
        message=message,
        detail=detail or {},
        request_id=request_id_var.get(),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LinkedInError)
    async def _linkedin_error(_request: Request, exc: LinkedInError) -> JSONResponse:
        # Expected upstream conditions: log at warning, no stack trace.
        logger.warning(
            "api.linkedin_error", code=exc.code, message=exc.message, detail=exc.detail
        )
        response = _render(exc.status_code, exc.code, exc.message, exc.detail)
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            response.headers["Retry-After"] = str(int(retry_after))
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _render(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "VALIDATION_ERROR",
            "The request body or query parameters were invalid.",
            {"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # Genuinely unexpected: log the trace, but never leak internals to the
        # caller — a stack trace can disclose paths and configuration.
        logger.exception("api.unhandled_exception", error=type(exc).__name__)
        return _render(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "INTERNAL_ERROR",
            "An unexpected internal error occurred.",
        )
