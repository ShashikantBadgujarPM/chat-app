"""Ports the conversation use cases depend on."""

from collections.abc import Callable, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from app.modules.conversations.domain.model import (
    Conversation,
    LastMessage,
    MemberProfile,
    MemberRole,
    Membership,
)
from app.modules.identity.application.ports import AuditPort
from app.platform.events import EventPublisher


class ConversationRepositoryPort(Protocol):
    async def insert_direct_on_conflict(
        self, *, direct_key: str, created_by: UUID
    ) -> tuple[UUID, bool]: ...

    async def create_group(self, *, title: str, created_by: UUID) -> UUID: ...

    async def get(
        self, conversation_id: UUID, *, for_update: bool = False
    ) -> Conversation | None: ...

    async def rename(self, conversation_id: UUID, title: str) -> None: ...

    async def list_for_user(
        self, user_id: UUID, *, after: tuple[datetime, UUID] | None, limit: int
    ) -> list[tuple[Conversation, Membership]]: ...

    async def get_active_membership(
        self, conversation_id: UUID, user_id: UUID
    ) -> Membership | None: ...

    async def add_member(self, conversation_id: UUID, user_id: UUID, role: MemberRole) -> None: ...

    async def upsert_member(
        self, conversation_id: UUID, user_id: UUID, role: MemberRole = MemberRole.MEMBER
    ) -> Membership | None: ...

    async def lock_owner_rows(self, conversation_id: UUID) -> list[UUID]: ...

    async def mark_left(self, conversation_id: UUID, user_id: UUID, *, now: datetime) -> bool: ...

    async def set_muted(self, conversation_id: UUID, user_id: UUID, muted: bool) -> None: ...

    async def count_active_members(self, conversation_id: UUID) -> int: ...

    async def active_member_ids(self, conversation_id: UUID) -> list[UUID]: ...

    async def list_members(
        self, conversation_id: UUID, *, user_ids: Sequence[UUID] | None = None
    ) -> list[MemberProfile]: ...

    async def member_summaries(
        self, conversation_ids: Sequence[UUID], *, viewer_id: UUID, preview_size: int
    ) -> dict[UUID, tuple[int, list[MemberProfile]]]: ...


class LastMessageReaderPort(Protocol):
    async def latest(self, pairs: Sequence[tuple[UUID, int]]) -> dict[UUID, LastMessage]:
        """For each (conversation_id, last_message_seq), the message at that seq."""
        ...


class UserDirectoryPort(Protocol):
    async def active_user_ids(self, user_ids: Sequence[UUID]) -> set[UUID]: ...


class ConversationsUnitOfWork(Protocol):
    @property
    def conversations(self) -> ConversationRepositoryPort: ...

    @property
    def users(self) -> UserDirectoryPort: ...

    @property
    def last_messages(self) -> LastMessageReaderPort: ...

    @property
    def events(self) -> EventPublisher: ...

    @property
    def audit(self) -> AuditPort: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


ConversationsUnitOfWorkFactory = Callable[[], ConversationsUnitOfWork]
