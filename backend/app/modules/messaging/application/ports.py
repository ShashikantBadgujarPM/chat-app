"""Ports the messaging use cases depend on."""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from app.modules.conversations.application.ports import ConversationRepositoryPort
from app.modules.messaging.domain.message import Message, MessageView
from app.platform.events import EventPublisher


class MessageRepositoryPort(Protocol):
    async def next_seq(self, conversation_id: UUID) -> int: ...

    async def insert_on_conflict_client_id(
        self,
        *,
        conversation_id: UUID,
        seq: int,
        sender_id: UUID,
        body: str,
        reply_to_id: UUID | None,
        client_message_id: UUID,
        scheduled_message_id: UUID | None,
    ) -> Message | None: ...

    async def edit(self, message_id: UUID, *, sender_id: UUID, body: str) -> Message | None: ...

    async def soft_delete(self, message_id: UUID, *, sender_id: UUID) -> Message | None: ...

    async def get(self, message_id: UUID) -> Message | None: ...

    async def get_by_client_id(
        self, sender_id: UUID, client_message_id: UUID
    ) -> Message | None: ...

    async def page_before(
        self, conversation_id: UUID, *, before_seq: int | None, limit: int
    ) -> list[Message]: ...

    async def page_after(
        self, conversation_id: UUID, *, after_seq: int, limit: int
    ) -> list[Message]: ...

    async def load_views(self, messages: Sequence[Message]) -> list[MessageView]: ...


class MessagingUnitOfWork(Protocol):
    @property
    def messages(self) -> MessageRepositoryPort: ...

    @property
    def conversations(self) -> ConversationRepositoryPort: ...

    @property
    def events(self) -> EventPublisher: ...

    def savepoint(self) -> AbstractAsyncContextManager[None]: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


MessagingUnitOfWorkFactory = Callable[[], MessagingUnitOfWork]
