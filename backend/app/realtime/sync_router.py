"""GET /api/v1/sync/events: the REST fallback for missed-event replay (M09).

Same semantics as the WS `sync.request`: the overlap window from `after_event_id`,
the caller's events only, and `reset_required` when replay can't be trusted.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.modules.identity.api.deps import Authenticated, authenticated_route_dependencies
from app.realtime.sync_service import ResetRequired, SyncService

router = APIRouter(
    prefix="/api/v1/sync", tags=["sync"], dependencies=authenticated_route_dependencies
)


class SyncEventsResponse(BaseModel):
    events: list[dict[str, Any]]
    has_more: bool
    reset_required: bool


@router.get("/events")
async def sync_events(
    request: Request,
    current: Authenticated,
    after_event_id: Annotated[int, Query(ge=0)],
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> SyncEventsResponse:
    sync: SyncService = request.app.state.sync
    try:
        events, has_more = await sync.page(current.user.id, after_event_id, limit)
    except ResetRequired:
        return SyncEventsResponse(events=[], has_more=False, reset_required=True)
    return SyncEventsResponse(
        events=[json.loads(e.to_frame()) for e in events], has_more=has_more, reset_required=False
    )
