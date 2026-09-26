"""Conversation request/response bodies (docs/design/07 §Conversations)."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

from app.modules.conversations.domain.model import (
    TITLE_MAX_LENGTH,
    ConversationView,
    MemberProfile,
)
from app.modules.identity.api.schemas import OFFLINE, UserPublic
from app.platform.schemas import RequestModel, UtcDateTime

Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX_LENGTH)
]


class CreateDirectRequest(RequestModel):
    user_id: UUID


class CreateGroupRequest(RequestModel):
    title: Title
    member_ids: Annotated[list[UUID], Field(default_factory=list, max_length=99)]


class RenameRequest(RequestModel):
    title: Title


class AddMembersRequest(RequestModel):
    user_ids: Annotated[list[UUID], Field(min_length=1, max_length=50)]


class MembershipUpdateRequest(RequestModel):
    notifications_muted: bool


class ReadCursorRequest(RequestModel):
    last_read_seq: Annotated[int, Field(ge=0)]


class ReadCursorResponse(BaseModel):
    last_read_seq: int
    unread_count: int


def _public(member: MemberProfile) -> UserPublic:
    return UserPublic(
        id=member.user_id,
        username=member.username,
        display_name=member.display_name,
        presence=OFFLINE,  # real presence arrives in M08
    )


class MemberOut(BaseModel):
    user: UserPublic
    role: Literal["owner", "member"]
    joined_at: UtcDateTime
    # The member's read position: only where read receipts apply (direct conversations
    # and groups under 20), else null. Lets "Seen" survive a reload (Q-016).
    last_read_seq: int | None = None

    @classmethod
    def from_profile(cls, member: MemberProfile, *, receipts: bool = False) -> "MemberOut":
        return cls(
            user=_public(member),
            role=member.role.value,
            joined_at=member.joined_at,
            last_read_seq=member.last_read_seq if receipts else None,
        )


class MembershipOut(MemberOut):
    notifications_muted: bool

    @classmethod
    def from_profile(cls, member: MemberProfile, *, receipts: bool = True) -> "MembershipOut":
        # The caller's own membership: their own read position is always theirs to see.
        return cls(
            user=_public(member),
            role=member.role.value,
            joined_at=member.joined_at,
            last_read_seq=member.last_read_seq if receipts else None,
            notifications_muted=member.notifications_muted,
        )


class ConversationOut(BaseModel):
    id: UUID
    type: Literal["direct", "group"]
    title: str | None
    created_at: UtcDateTime
    last_message_seq: int
    members_preview: list[UserPublic]
    member_count: int
    my_role: Literal["owner", "member"]

    @classmethod
    def from_view(cls, view: ConversationView) -> "ConversationOut":
        c = view.conversation
        return cls(
            id=c.id,
            type=c.type.value,
            title=c.title,
            created_at=c.created_at,
            last_message_seq=c.last_message_seq,
            members_preview=[_public(m) for m in view.members_preview],
            member_count=view.member_count,
            my_role=view.my_role.value,
        )


class LastMessageOut(BaseModel):
    """07 `MessageBrief`."""

    id: UUID
    seq: int
    sender_id: UUID | None
    body_preview: str
    deleted: bool


class ConversationSummaryOut(ConversationOut):
    # Unread counts are filled in by M07.
    last_message: LastMessageOut | None = None
    unread_count: int = 0
    unread_mention_count: int = 0
    last_activity_at: UtcDateTime
    my_last_read_seq: int

    @classmethod
    def from_view(cls, view: ConversationView) -> "ConversationSummaryOut":
        base = ConversationOut.from_view(view)
        last = view.last_message
        return cls(
            **base.model_dump(),
            last_message=(
                LastMessageOut(
                    id=last.id,
                    seq=last.seq,
                    sender_id=last.sender_id,
                    body_preview=last.body_preview,
                    deleted=last.deleted,
                )
                if last is not None
                else None
            ),
            last_activity_at=view.conversation.last_activity_at,
            my_last_read_seq=view.my_last_read_seq,
            unread_count=view.unread_count,
        )


class MembersResponse(BaseModel):
    items: list[MemberOut]


class AddMembersResponse(BaseModel):
    added: list[MemberOut]
    already_members: list[UUID]
