"""Messages (docs/design/04 §8.1-8.2, 07 §Messages). Pure domain code."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.modules.messaging.domain.errors import BodyEmpty, BodyTooLong

BODY_MAX_LENGTH = 4000
PREVIEW_LENGTH = 120


def normalize_body(body: str) -> str:
    """Trim surrounding whitespace and enforce 1..4000 characters."""
    trimmed = body.strip()
    if not trimmed:
        raise BodyEmpty()
    if len(trimmed) > BODY_MAX_LENGTH:
        raise BodyTooLong()
    return trimmed


@dataclass(frozen=True, slots=True)
class Message:
    id: UUID
    conversation_id: UUID
    seq: int
    sender_id: UUID | None
    body: str | None  # None once deleted (a tombstone)
    reply_to_id: UUID | None
    client_message_id: UUID
    scheduled_message_id: UUID | None
    created_at: datetime
    edited_at: datetime | None
    deleted_at: datetime | None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def can_be_edited_by(self, user_id: UUID) -> bool:
        return self.sender_id == user_id and not self.is_deleted

    @property
    def preview(self) -> str:
        """Up to 120 characters for reply previews; empty for a tombstone."""
        if self.body is None:
            return ""
        return (
            self.body if len(self.body) <= PREVIEW_LENGTH else self.body[: PREVIEW_LENGTH - 1] + "…"
        )


@dataclass(frozen=True, slots=True)
class SenderProfile:
    id: UUID
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class MessageView:
    """A message with what the API shows alongside it."""

    message: Message
    sender: SenderProfile | None
    reply_to: Message | None
    mentions: list[UUID]


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: list[MessageView]
    has_more: bool


@dataclass(frozen=True, slots=True)
class AroundPage:
    items: list[MessageView]  # ascending by seq
    has_more_before: bool
    has_more_after: bool
