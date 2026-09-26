"""Ports the scheduling use cases depend on."""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.modules.conversations.application.ports import UserDirectoryPort
from app.modules.identity.application.ports import AuditPort
from app.modules.messaging.application.ports import MessagingUnitOfWork
from app.modules.scheduling.domain.scheduled_message import ScheduledMessage, ScheduledStatus


class ScheduledMessageRepositoryPort(Protocol):
    async def insert_on_conflict_client_id(
        self,
        *,
        conversation_id: UUID,
        sender_id: UUID,
        body: str,
        reply_to_id: UUID | None,
        client_message_id: UUID,
        scheduled_at_utc: datetime,
        sender_timezone: str,
    ) -> ScheduledMessage | None: ...

    async def get(self, scheduled_id: UUID) -> ScheduledMessage | None: ...

    async def get_by_client_id(self, client_message_id: UUID) -> ScheduledMessage | None: ...

    async def count_pending(self, sender_id: UUID) -> int: ...

    async def list_for_sender(
        self,
        sender_id: UUID,
        *,
        status: ScheduledStatus | None,
        conversation_id: UUID | None,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[ScheduledMessage]: ...

    async def update_pending(
        self, scheduled_id: UUID, sender_id: UUID, changes: Mapping[str, Any]
    ) -> ScheduledMessage | None: ...

    async def cancel(self, scheduled_id: UUID, sender_id: UUID) -> ScheduledMessage | None: ...

    async def reset_failed(
        self, scheduled_id: UUID, sender_id: UUID, *, scheduled_at_utc: datetime
    ) -> ScheduledMessage | None: ...

    async def sent_message_ids(self, scheduled_ids: Sequence[UUID]) -> dict[UUID, UUID]: ...

    async def claim_due(
        self, batch_size: int
    ) -> tuple[datetime | None, list[ScheduledMessage]]: ...

    async def mark_sent(self, scheduled_id: UUID) -> ScheduledMessage: ...

    async def mark_retry(
        self, scheduled_id: UUID, *, next_attempt_at: datetime, error: str
    ) -> ScheduledMessage: ...

    async def mark_failed(
        self, scheduled_id: UUID, *, reason: str, count_attempt: bool
    ) -> ScheduledMessage: ...


class SchedulingUnitOfWork(MessagingUnitOfWork, Protocol):
    """Everything the messaging write path needs, so the worker can hand this same
    transaction to MessagingService.create_message (09 §17.3)."""

    @property
    def scheduled(self) -> ScheduledMessageRepositoryPort: ...

    @property
    def users(self) -> UserDirectoryPort: ...

    @property
    def audit(self) -> AuditPort: ...


SchedulingUnitOfWorkFactory = Callable[[], SchedulingUnitOfWork]
