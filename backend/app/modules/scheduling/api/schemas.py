"""Scheduled-message request/response bodies (docs/design/07 §Scheduled messages)."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, StringConstraints

from app.modules.messaging.api.schemas import RawBody
from app.modules.scheduling.application.payloads import scheduled_payload
from app.modules.scheduling.domain.scheduled_message import (
    TIMEZONE_MAX_LENGTH,
    ScheduledMessageView,
)
from app.platform.schemas import RequestModel, UtcDateTime

Timezone = Annotated[str, StringConstraints(max_length=TIMEZONE_MAX_LENGTH)]

# `scheduled_at` is parsed as a plain datetime on purpose: a naive value gets the
# specific 422 `naive_datetime` from the domain, not a generic validation error.


class CreateScheduledRequest(RequestModel):
    client_message_id: UUID
    conversation_id: UUID
    body: RawBody
    reply_to_id: UUID | None = None
    scheduled_at: datetime
    timezone: Timezone | None = None  # defaults to the caller's profile time zone


class UpdateScheduledRequest(RequestModel):
    body: RawBody | None = None
    scheduled_at: datetime | None = None
    timezone: Timezone | None = None
    # Sent as null: remove the reply. Left out: unchanged (see model_fields_set).
    reply_to_id: UUID | None = None


class RetryScheduledRequest(RequestModel):
    scheduled_at: datetime | None = None  # default: as soon as allowed (now + 30 s)


class ScheduledMessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    body: str
    reply_to_id: UUID | None
    scheduled_at: UtcDateTime
    timezone: str
    status: Literal["pending", "sent", "cancelled", "failed"]
    attempts: int
    last_error: str | None
    sent_message_id: UUID | None
    sent_at: UtcDateTime | None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    cancelled_at: UtcDateTime | None

    @classmethod
    def from_view(cls, view: ScheduledMessageView) -> "ScheduledMessageOut":
        # Validated from the event payload, so REST and WS can't disagree on the shape.
        return cls.model_validate(scheduled_payload(view))
