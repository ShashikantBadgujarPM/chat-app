"""/api/v1/auth (docs/design/07 §Auth) and the temporary /api/v1/users/me (M02)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Response, status

from app.config import Settings
from app.modules.identity.api.deps import (
    REFRESH_COOKIE,
    Authenticated,
    Client,
    IdentityDeps,
    authenticated_route_dependencies,
    clear_refresh_cookie,
    get_settings_from_app,
    require_csrf_header,
    set_refresh_cookie,
)
from app.modules.identity.api.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    SessionOut,
    SessionsResponse,
    TokenResponse,
    UserMe,
)
from app.modules.identity.application.services import (
    ChangePassword,
    ListSessions,
    Login,
    Logout,
    LogoutAll,
    RefreshSession,
    RegisterUser,
    RevokeSession,
)
from app.modules.identity.domain.errors import InvalidRefreshToken
from app.platform.rate_limit import client_ip, rate_limited

AppSettings = Annotated[Settings, Depends(get_settings_from_app)]
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE)]

_register_limit = rate_limited(
    "auth.register",
    client_ip,
    limit=lambda r: (r.app.state.settings.rate_limit_register_per_hour, 3600),
)
_login_limit = rate_limited(
    "auth.login",
    client_ip,
    limit=lambda r: (r.app.state.settings.rate_limit_login_per_minute, 60),
)
_refresh_limit = rate_limited(
    "auth.refresh",
    client_ip,
    limit=lambda r: (r.app.state.settings.rate_limit_refresh_per_minute, 60),
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_register_limit)],
)
async def register(body: RegisterRequest, deps: IdentityDeps) -> UserMe:
    user = await RegisterUser(deps)(
        username=body.username,
        email=str(body.email),
        display_name=body.display_name,
        password=body.password,
        timezone=body.timezone,
    )
    return UserMe.from_user(user)


@router.post("/login", dependencies=[Depends(_login_limit)])
async def login(
    body: LoginRequest,
    response: Response,
    deps: IdentityDeps,
    client: Client,
    settings: AppSettings,
) -> LoginResponse:
    result = await Login(deps)(
        identifier=body.username_or_email, password=body.password, client=client
    )
    set_refresh_cookie(
        response,
        result.tokens.refresh_token,
        expires_at=result.tokens.refresh_expires_at,
        now=deps.clock.now(),
        settings=settings,
    )
    return LoginResponse(
        access_token=result.tokens.access_token,
        expires_in=result.tokens.expires_in,
        user=UserMe.from_user(result.user),
    )


@router.post(
    "/refresh",
    dependencies=[Depends(require_csrf_header), Depends(_refresh_limit)],
)
async def refresh(
    response: Response,
    deps: IdentityDeps,
    client: Client,
    settings: AppSettings,
    refresh_token: RefreshCookie = None,
) -> TokenResponse:
    if not refresh_token:
        raise InvalidRefreshToken()
    tokens = await RefreshSession(deps)(refresh_token=refresh_token, client=client)
    set_refresh_cookie(
        response,
        tokens.refresh_token,
        expires_at=tokens.refresh_expires_at,
        now=deps.clock.now(),
        settings=settings,
    )
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf_header)],
)
async def logout(
    response: Response,
    deps: IdentityDeps,
    client: Client,
    settings: AppSettings,
    refresh_token: RefreshCookie = None,
) -> None:
    await Logout(deps)(refresh_token=refresh_token, client=client)
    clear_refresh_cookie(response, settings)


authenticated = APIRouter(
    prefix="/api/v1/auth", tags=["auth"], dependencies=authenticated_route_dependencies
)


@authenticated.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    response: Response,
    current: Authenticated,
    deps: IdentityDeps,
    client: Client,
    settings: AppSettings,
) -> None:
    await LogoutAll(deps)(user_id=current.user.id, client=client)
    clear_refresh_cookie(response, settings)


@authenticated.get("/sessions")
async def list_sessions(current: Authenticated, deps: IdentityDeps) -> SessionsResponse:
    views = await ListSessions(deps)(user_id=current.user.id, current_session_id=current.session_id)
    return SessionsResponse(items=[SessionOut.from_view(view) for view in views])


@authenticated.delete("/sessions/{family_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    family_id: UUID, current: Authenticated, deps: IdentityDeps, client: Client
) -> None:
    await RevokeSession(deps)(user_id=current.user.id, family_id=family_id, client=client)


@authenticated.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest, current: Authenticated, deps: IdentityDeps, client: Client
) -> None:
    await ChangePassword(deps)(
        user=current.user,
        current_session_id=current.session_id,
        current_password=body.current_password,
        new_password=body.new_password,
        client=client,
    )
