"""Conversations and membership (docs/design/04 §8.1-8.2). Pure domain code."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

GROUP_MEMBER_LIMIT = 100
TITLE_MAX_LENGTH = 100


class ConversationType(StrEnum):
    DIRECT = "direct"
    GROUP = "group"


class MemberRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"


def direct_key(a: UUID, b: UUID) -> str:
    """The key that makes a direct conversation unique per unordered pair (R-2)."""
    low, high = sorted((str(a), str(b)))
    return f"{low}:{high}"


@dataclass(frozen=True, slots=True)
class Conversation:
    id: UUID
    type: ConversationType
    title: str | None
    created_by: UUID | None
    last_message_seq: int
    last_activity_at: datetime
    created_at: datetime

    @property
    def is_group(self) -> bool:
        return self.type is ConversationType.GROUP


@dataclass(frozen=True, slots=True)
class Membership:
    conversation_id: UUID
    user_id: UUID
    role: MemberRole
    last_read_seq: int
    notifications_muted: bool
    joined_at: datetime
    left_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.left_at is None

    @property
    def is_owner(self) -> bool:
        return self.role is MemberRole.OWNER


@dataclass(frozen=True, slots=True)
class MemberProfile:
    """A member together with the public fields of their user."""

    user_id: UUID
    username: str
    display_name: str
    role: MemberRole
    joined_at: datetime
    notifications_muted: bool
    last_read_seq: int = 0


@dataclass(frozen=True, slots=True)
class LastMessage:
    """The conversation list's preview of the latest message (07 `MessageBrief`)."""

    id: UUID
    seq: int
    sender_id: UUID | None
    body_preview: str
    deleted: bool


@dataclass(frozen=True, slots=True)
class ConversationView:
    """A conversation as one member sees it (07 `Conversation`)."""

    conversation: Conversation
    members_preview: list[MemberProfile]
    member_count: int
    my_role: MemberRole
    my_last_read_seq: int
    last_message: LastMessage | None = None
    unread_count: int = 0
