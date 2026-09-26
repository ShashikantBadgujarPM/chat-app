"""The conversations UnitOfWork: one transaction with the module's repositories."""

from collections.abc import Sequence
from typing import Self
from uuid import UUID

from app.modules.conversations.domain.model import LastMessage
from app.modules.conversations.infrastructure.repository import (
    ConversationRepository,
    UserDirectory,
)
from app.modules.messaging.infrastructure.repository import MessageRepository
from app.platform.audit import AuditLogger
from app.platform.db import SessionFactory, UnitOfWork
from app.platform.events import EventPublisher
from app.realtime.publisher import OutboxEventPublisher


class LastMessageReader:
    """Adapts the messaging repository to the conversations module's own type."""

    def __init__(self, messages: MessageRepository) -> None:
        self._messages = messages

    async def latest(self, pairs: Sequence[tuple[UUID, int]]) -> dict[UUID, LastMessage]:
        found = await self._messages.latest_by_conversation(pairs)
        return {
            cid: LastMessage(
                id=m.id,
                seq=m.seq,
                sender_id=m.sender_id,
                body_preview=m.preview,
                deleted=m.is_deleted,
            )
            for cid, m in found.items()
        }


class SqlConversationsUnitOfWork(UnitOfWork):
    conversations: ConversationRepository
    users: UserDirectory
    last_messages: LastMessageReader
    events: EventPublisher
    audit: AuditLogger

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self.conversations = ConversationRepository(self.session)
        self.users = UserDirectory(self.session)
        self.last_messages = LastMessageReader(MessageRepository(self.session))
        self.events = OutboxEventPublisher(self.session)
        self.audit = AuditLogger(self.session)
        return self
