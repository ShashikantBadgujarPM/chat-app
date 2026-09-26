"""Message request/response bodies (docs/design/07 §Messages)."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, StringConstraints

from app.modules.identity.api.schemas import UserPublic
from app.modules.messaging.application.payloads import message_payload
from app.modules.messaging.domain.message import MessageView
from app.platform.schemas import RequestModel, UtcDateTime

# The domain enforces 1..4000 characters after trimming (body_empty / body_too_long).
# This cap only bounds the request before that.
RawBody = Annotated[str, StringConstraints(max_length=16_000)]


class SendMessageRequest(RequestModel):
    client_message_id: UUID
    body: RawBody
    reply_to_id: UUID | None = None


class EditMessageRequest(RequestModel):
    body: RawBody


class MessageBrief(BaseModel):
    id: UUID
    seq: int
    sender_id: UUID | None
    body_preview: str
    deleted: bool


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    seq: int
    sender: UserPublic | None
    body: str | None
    reply_to: MessageBrief | None
    mentions: list[UUID]
    created_at: UtcDateTime
    edited_at: UtcDateTime | None
    deleted_at: UtcDateTime | None
    scheduled_message_id: UUID | None
    client_message_id: UUID

    @classmethod
    def from_view(cls, view: MessageView) -> "MessageOut":
        # Validated from the event payload, so REST and WS can't disagree on the shape.
        return cls.model_validate(message_payload(view))


class HistoryResponse(BaseModel):
    items: list[MessageOut]
    has_more: bool


class AroundResponse(BaseModel):
    items: list[MessageOut]
    has_more_before: bool
    has_more_after: bool
