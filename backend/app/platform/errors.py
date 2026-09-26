"""The application exception hierarchy and its single REST translation point.

Domain and application code raise these; only the handlers registered here turn them
into HTTP responses (docs/design/04 §8.3, docs/design/10 §18). Route handlers never
catch exceptions just to reformat them.

Every non-2xx response uses the envelope from docs/design/07 §13.1:
    {"error": {"code", "message", "details", "request_id"}}
"""

import logging
from collections.abc import Mapping
from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.platform.logging import get_log_context

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class. Raising AppError itself means a bug, so it maps to 500."""

    status_code: ClassVar[int] = 500
    default_code: ClassVar[str] = "internal_error"
    default_message: ClassVar[str] = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: Any = None,
    ) -> None:
        self.code = code or self.default_code
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)

    def headers(self) -> dict[str, str]:
        return {}


class DomainError(AppError):
    """A business rule was violated."""

    status_code = 422
    default_code = "domain_error"
    default_message = "The request violates a business rule."


class ValidationError(DomainError):
    status_code = 422
    default_code = "validation_error"
    default_message = "The request is invalid."


class ConflictError(DomainError):
    status_code = 409
    default_code = "conflict"
    default_message = "The request conflicts with the current state of the resource."


class InvariantViolation(DomainError):  # noqa: N818 - name fixed by docs/design/04 §8.3
    status_code = 409
    default_code = "invariant_violation"
    default_message = "The request would break a rule that must always hold."


class NotFoundError(AppError):
    status_code = 404
    default_code = "not_found"
    default_message = "The resource was not found."


class AuthenticationError(AppError):
    status_code = 401
    default_code = "unauthenticated"
    default_message = "Authentication is required."

    def headers(self) -> dict[str, str]:
        return {"WWW-Authenticate": "Bearer"}


class _RetryAfterMixin:
    retry_after_seconds: int | None

    def _retry_after_headers(self) -> dict[str, str]:
        if self.retry_after_seconds is None:
            return {}
        return {"Retry-After": str(self.retry_after_seconds)}


class AccountLockedError(_RetryAfterMixin, AuthenticationError):
    status_code = 423
    default_code = "account_locked"
    default_message = "The account is temporarily locked."

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after_seconds: int | None = None,
        code: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.retry_after_seconds = retry_after_seconds

    def headers(self) -> dict[str, str]:
        # A lock is not a missing credential, so no WWW-Authenticate challenge.
        return self._retry_after_headers()


class AuthorizationError(AppError):
    status_code = 403
    default_code = "forbidden"
    default_message = "You are not allowed to perform this action."


class RateLimitedError(_RetryAfterMixin, AppError):
    status_code = 429
    default_code = "rate_limited"
    default_message = "Too many requests."

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after_seconds: int | None = None,
        code: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.retry_after_seconds = retry_after_seconds

    def headers(self) -> dict[str, str]:
        return self._retry_after_headers()


# --- envelope -------------------------------------------------------------------------

_HTTP_STATUS_CODES: dict[int, tuple[str, str]] = {
    400: ("bad_request", "The request is malformed."),
    404: ("not_found", "The resource was not found."),
    405: ("method_not_allowed", "The method is not allowed for this resource."),
    413: ("payload_too_large", "The request body is too large."),
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Any = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": get_log_context().request_id,
        }
    }
    return JSONResponse(body, status_code=status_code, headers=headers)


def internal_error_response() -> JSONResponse:
    # Never include exception text, SQL or stack traces, whatever the environment.
    return error_response(500, AppError.default_code, AppError.default_message)


# --- handlers -------------------------------------------------------------------------


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - registered for AppError only
    if exc.status_code >= 500:
        logger.error(
            "Application error",
            exc_info=exc,
            extra={"event": "http.app_error", "code": exc.code},
        )
        return internal_error_response()
    return error_response(
        exc.status_code, exc.code, exc.message, details=exc.details, headers=exc.headers()
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for this type only
    # Reshape Pydantic's errors so internal model structure isn't exposed.
    details = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ())]
        if location and location[0] == "body":
            location = location[1:]
        details.append({"field": ".".join(location) or None, "issue": error.get("msg")})
    return error_response(
        422, "validation_error", "The request failed validation.", details=details
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered for this type only
    code, message = _HTTP_STATUS_CODES.get(exc.status_code, ("http_error", "Request failed."))
    return error_response(exc.status_code, code, message, headers=exc.headers)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)


class UnhandledExceptionMiddleware:
    """Turns any exception that escapes the app into the generic 500 envelope.

    Starlette's own catch-all runs outside every user middleware, where the request
    id is no longer bound. Catching here keeps the id in both the log line and the
    response body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("Unhandled exception", extra={"event": "http.unhandled_exception"})
            if response_started:
                # Too late to send a different response; let the server close it.
                raise
            await internal_error_response()(scope, receive, send_wrapper)
