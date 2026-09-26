"""/api/v1/scheduled-messages (docs/design/07 §Scheduled messages).

Top-level rather than nested under a conversation: the main view is "all my scheduled
messages". Every route is the caller's own rows only; other users' rows are 404.
"""

from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status

from app.modules.identity.api.deps import Authenticated, authenticated_route_dependencies
from app.modules.scheduling.api.schemas import (
    CreateScheduledRequest,
    RetryScheduledRequest,
    ScheduledMessageOut,
    UpdateScheduledRequest,
)
from app.modules.scheduling.application.scheduling_service import (
    UNSET,
    SchedulePolicy,
    SchedulingService,
)
from app.modules.scheduling.domain.scheduled_message import ScheduledStatus
from app.modules.scheduling.infrastructure.unit_of_work import SqlSchedulingUnitOfWork
from app.platform.pagination import Page


def get_scheduling_service(request: Request) -> SchedulingService:
    state = request.app.state
    settings = state.settings
    return SchedulingService(
        lambda: SqlSchedulingUnitOfWork(state.session_factory),
        state.clock,
        SchedulePolicy(
            min_lead=timedelta(seconds=settings.schedule_min_lead_seconds),
            max_horizon=timedelta(days=settings.schedule_max_horizon_days),
            max_pending_per_user=settings.schedule_max_pending_per_user,
        ),
    )


Scheduling = Annotated[SchedulingService, Depends(get_scheduling_service)]

router = APIRouter(
    prefix="/api/v1/scheduled-messages",
    tags=["scheduled-messages"],
    dependencies=authenticated_route_dependencies,
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Idempotent replay: the original scheduled message"}},
)
async def create_scheduled_message(
    body: CreateScheduledRequest, response: Response, current: Authenticated, service: Scheduling
) -> ScheduledMessageOut:
    view, created = await service.create(
        caller_id=current.user.id,
        conversation_id=body.conversation_id,
        body=body.body,
        reply_to_id=body.reply_to_id,
        scheduled_at=body.scheduled_at,
        timezone=body.timezone or current.user.timezone,
        client_message_id=body.client_message_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return ScheduledMessageOut.from_view(view)


@router.get("")
async def list_scheduled_messages(
    current: Authenticated,
    service: Scheduling,
    status_filter: Annotated[ScheduledStatus | None, Query(alias="status")] = None,
    conversation_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> Page[ScheduledMessageOut]:
    page = await service.list_for_user(
        caller_id=current.user.id,
        status=status_filter,
        conversation_id=conversation_id,
        cursor=cursor,
        limit=limit,
    )
    return Page(
        items=[ScheduledMessageOut.from_view(v) for v in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{scheduled_id}")
async def get_scheduled_message(
    scheduled_id: UUID, current: Authenticated, service: Scheduling
) -> ScheduledMessageOut:
    view = await service.get(caller_id=current.user.id, scheduled_id=scheduled_id)
    return ScheduledMessageOut.from_view(view)


@router.patch("/{scheduled_id}")
async def update_scheduled_message(
    scheduled_id: UUID, body: UpdateScheduledRequest, current: Authenticated, service: Scheduling
) -> ScheduledMessageOut:
    view = await service.update(
        caller_id=current.user.id,
        scheduled_id=scheduled_id,
        body=body.body,
        scheduled_at=body.scheduled_at,
        timezone=body.timezone,
        reply_to_id=body.reply_to_id if "reply_to_id" in body.model_fields_set else UNSET,
    )
    return ScheduledMessageOut.from_view(view)


@router.post("/{scheduled_id}/cancel")
async def cancel_scheduled_message(
    scheduled_id: UUID, current: Authenticated, service: Scheduling
) -> ScheduledMessageOut:
    view = await service.cancel(caller_id=current.user.id, scheduled_id=scheduled_id)
    return ScheduledMessageOut.from_view(view)


@router.post("/{scheduled_id}/retry")
async def retry_scheduled_message(
    scheduled_id: UUID,
    current: Authenticated,
    service: Scheduling,
    body: Annotated[RetryScheduledRequest | None, Body()] = None,
) -> ScheduledMessageOut:
    view = await service.retry(
        caller_id=current.user.id,
        scheduled_id=scheduled_id,
        scheduled_at=body.scheduled_at if body is not None else None,
    )
    return ScheduledMessageOut.from_view(view)
