"""/api/v1/users (docs/design/07 §Users)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.config import Settings
from app.modules.identity.api.deps import (
    Authenticated,
    Client,
    IdentityDeps,
    authenticated_route_dependencies,
    clear_refresh_cookie,
    get_settings_from_app,
)
from app.modules.identity.api.schemas import (
    DeleteMeRequest,
    PresenceItem,
    PresenceListResponse,
    UpdateMeRequest,
    UserMe,
    UserPublic,
)
from app.modules.identity.application.profile_service import ProfileService
from app.modules.presence.api.deps import presence_for
from app.platform.errors import ValidationError
from app.platform.pagination import Page

MAX_PRESENCE_IDS = 100

router = APIRouter(
    prefix="/api/v1/users", tags=["users"], dependencies=authenticated_route_dependencies
)


@router.get("/me")
async def get_me(current: Authenticated) -> UserMe:
    return UserMe.from_user(current.user)


@router.patch("/me")
async def update_me(body: UpdateMeRequest, current: Authenticated, deps: IdentityDeps) -> UserMe:
    user = await ProfileService(deps).update_me(
        current.user.id, display_name=body.display_name, timezone=body.timezone
    )
    return UserMe.from_user(user)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    body: DeleteMeRequest,
    response: Response,
    current: Authenticated,
    deps: IdentityDeps,
    client: Client,
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> None:
    await ProfileService(deps).delete_me(current.user, password=body.password, client=client)
    clear_refresh_cookie(response, settings)


@router.get("/presence")
async def get_presence(
    request: Request,
    current: Authenticated,
    ids: Annotated[str, Query(max_length=100 * 37)],
) -> PresenceListResponse:
    """Current presence for up to 100 users (07 §Users). Clients call it after
    (re)connecting, since presence events are ephemeral and never replayed."""
    del current
    try:
        user_ids = [UUID(part) for part in ids.split(",") if part.strip()]
    except ValueError as exc:
        raise ValidationError(code="invalid_ids", message="ids must be UUIDs.") from exc
    if not 1 <= len(user_ids) <= MAX_PRESENCE_IDS:
        raise ValidationError(code="invalid_ids", message=f"Ask for 1 to {MAX_PRESENCE_IDS} ids.")
    found = await presence_for(request, user_ids)
    return PresenceListResponse(
        items=[
            PresenceItem(user_id=uid, status=p.status, last_seen_at=p.last_seen_at)
            for uid, p in found.items()
        ]
    )


@router.get("")
async def search_users(
    request: Request,
    current: Authenticated,
    deps: IdentityDeps,
    q: Annotated[str, Query(max_length=64)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> Page[UserPublic]:
    page = await ProfileService(deps).search_users(
        caller_id=current.user.id, query=q, limit=limit, cursor=cursor
    )
    presence = await presence_for(request, [u.id for u in page.users])
    return Page(
        items=[UserPublic.from_user(u, presence[u.id]) for u in page.users],
        next_cursor=page.next_cursor,
    )


@router.get("/{user_id}")
async def get_user(
    user_id: UUID, request: Request, current: Authenticated, deps: IdentityDeps
) -> UserPublic:
    del current  # authentication only
    user = await ProfileService(deps).get_public_profile(user_id)
    presence = await presence_for(request, [user.id])
    return UserPublic.from_user(user, presence[user.id])
