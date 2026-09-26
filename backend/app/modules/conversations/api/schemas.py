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

    @classmethod
    def from_profile(cls, member: MemberProfile) -> "MemberOut":
        return cls(user=_public(member), role=member.role.value, joined_at=member.joined_at)


class MembershipOut(MemberOut):
    notifications_muted: bool

    @classmethod
    def from_profile(cls, member: MemberProfile) -> "MembershipOut":
        return cls(
            user=_public(member),
            role=member.role.value,
            joined_at=member.joined_at,
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


class ConversationSummaryOut(ConversationOut):
    # Message-dependent fields are filled in by M05 (last_message) and M07 (unread).
    last_message: None = None
    unread_count: int = 0
    unread_mention_count: int = 0
    last_activity_at: UtcDateTime
    my_last_read_seq: int

    @classmethod
    def from_view(cls, view: ConversationView) -> "ConversationSummaryOut":
        base = ConversationOut.from_view(view)
        return cls(
            **base.model_dump(),
            last_activity_at=view.conversation.last_activity_at,
            my_last_read_seq=view.my_last_read_seq,
        )


class MembersResponse(BaseModel):
    items: list[MemberOut]


class AddMembersResponse(BaseModel):
    added: list[MemberOut]
    already_members: list[UUID]
