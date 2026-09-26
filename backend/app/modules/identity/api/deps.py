"""FastAPI dependencies for identity: service wiring, the current user, CSRF, cookies."""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.modules.identity.application.services import (
    AuthenticateAccessToken,
    ClientInfo,
    CurrentUser,
    IdentityDependencies,
)
from app.modules.identity.domain.errors import InvalidRefreshToken, RefreshTokenReused
from app.modules.identity.infrastructure.unit_of_work import SqlIdentityUnitOfWork
from app.platform.errors import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    app_error_response,
)
from app.platform.logging import update_log_context
from app.platform.rate_limit import client_ip, rate_limited

REFRESH_COOKIE = "rt"
REFRESH_COOKIE_PATH = "/api/v1/auth"
CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "chat-app"

_bearer = HTTPBearer(auto_error=False)


def get_settings_from_app(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_identity_deps(request: Request) -> IdentityDependencies:
    state = request.app.state
    return IdentityDependencies(
        uow_factory=lambda: SqlIdentityUnitOfWork(state.session_factory),
        hasher=state.password_hasher,
        tokens=state.token_issuer,
        clock=state.clock,
        refresh_token_ttl=timedelta(days=state.settings.refresh_token_ttl_days),
    )


IdentityDeps = Annotated[IdentityDependencies, Depends(get_identity_deps)]


def get_client_info(request: Request) -> ClientInfo:
    return ClientInfo(ip_address=client_ip(request), user_agent=request.headers.get("user-agent"))


Client = Annotated[ClientInfo, Depends(get_client_info)]


async def get_current_user(
    request: Request,
    deps: IdentityDeps,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError(code="not_authenticated", message="Authentication is required.")
    current = await AuthenticateAccessToken(deps)(credentials.credentials)
    user_id = str(current.user.id)
    # For log lines inside this request, and for the access-log line written after it.
    update_log_context(user_id=user_id)
    request.state.user_id = user_id
    return current


Authenticated = Annotated[CurrentUser, Depends(get_current_user)]


async def _authenticated_rate_limit(request: Request, current: Authenticated) -> None:
    del current  # resolved first so request.state.user_id is set
    await rate_limited(
        "authenticated",
        lambda r: r.state.user_id,
        limit=lambda r: (r.app.state.settings.rate_limit_authenticated_per_minute, 60),
    )(request)


# Use on routers of authenticated endpoints: authentication + the per-user limit.
authenticated_route_dependencies = [Depends(_authenticated_rate_limit)]


async def require_csrf_header(request: Request) -> None:
    """Cookie-authenticated endpoints only (docs/design/06 §11.2). Cross-site forms
    can't send a custom header without a CORS preflight, which our policy rejects."""
    if request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
        raise AuthorizationError(
            code="csrf_header_missing",
            message=f"The {CSRF_HEADER} header is required.",
        )


def set_refresh_cookie(
    response: Response, token: str, *, expires_at: datetime, now: datetime, settings: Settings
) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=max(0, int((expires_at - now).total_seconds())),
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def register_identity_exception_handlers(app: FastAPI) -> None:
    """Errors that end the session also delete the refresh cookie."""

    async def handler(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, AppError)  # noqa: S101 - registered for AppError subclasses
        response = app_error_response(exc)
        clear_refresh_cookie(response, request.app.state.settings)
        return response

    for error_type in (InvalidRefreshToken, RefreshTokenReused):
        app.add_exception_handler(error_type, handler)
