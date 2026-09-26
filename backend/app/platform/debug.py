"""Temporary test-only route that raises each error type (docs/milestones/M00).

`create_app` includes this router only when ENV=test, so it never exists in a
development or production process.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import NoReturn

from fastapi import APIRouter

from app.platform.errors import (
    AccountLockedError,
    AppError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    DomainError,
    InvariantViolation,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)

router = APIRouter(prefix="/api/v1/_debug", tags=["debug"], include_in_schema=False)


class ErrorKind(StrEnum):
    APP = "app"
    DOMAIN = "domain"
    VALIDATION = "validation"
    CONFLICT = "conflict"
    INVARIANT = "invariant"
    NOT_FOUND = "not_found"
    AUTHENTICATION = "authentication"
    ACCOUNT_LOCKED = "account_locked"
    AUTHORIZATION = "authorization"
    RATE_LIMITED = "rate_limited"
    UNHANDLED = "unhandled"


# Factories, so each request raises a fresh exception with its own traceback.
_ERRORS: dict[ErrorKind, Callable[[], AppError]] = {
    ErrorKind.APP: lambda: AppError("debug: internal detail that must not leak"),
    ErrorKind.DOMAIN: lambda: DomainError(),
    ErrorKind.VALIDATION: lambda: ValidationError(details=[{"field": "body", "issue": "too long"}]),
    ErrorKind.CONFLICT: lambda: ConflictError(code="not_pending"),
    ErrorKind.INVARIANT: lambda: InvariantViolation(code="cannot_remove_last_owner"),
    ErrorKind.NOT_FOUND: lambda: NotFoundError(code="conversation_not_found"),
    ErrorKind.AUTHENTICATION: lambda: AuthenticationError(code="invalid_token"),
    ErrorKind.ACCOUNT_LOCKED: lambda: AccountLockedError(retry_after_seconds=900),
    ErrorKind.AUTHORIZATION: lambda: AuthorizationError(code="not_owner"),
    ErrorKind.RATE_LIMITED: lambda: RateLimitedError(retry_after_seconds=30),
}


@router.get("/error/{kind}", response_model=None)
async def raise_error(kind: ErrorKind) -> NoReturn:
    if kind is ErrorKind.UNHANDLED:
        raise RuntimeError("debug: internal detail that must not leak")
    raise _ERRORS[kind]()
