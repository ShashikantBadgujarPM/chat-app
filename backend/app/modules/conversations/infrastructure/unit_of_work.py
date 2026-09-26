"""The conversations UnitOfWork: one transaction with the module's repositories."""

from typing import Self

from app.modules.conversations.infrastructure.repository import (
    ConversationRepository,
    UserDirectory,
)
from app.platform.audit import AuditLogger
from app.platform.db import SessionFactory, UnitOfWork
from app.platform.events import EventPublisher, NoOpEventPublisher


class SqlConversationsUnitOfWork(UnitOfWork):
    conversations: ConversationRepository
    users: UserDirectory
    events: EventPublisher
    audit: AuditLogger

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self.conversations = ConversationRepository(self.session)
        self.users = UserDirectory(self.session)
        # Replaced by the transactional outbox publisher in M06.
        self.events = NoOpEventPublisher()
        self.audit = AuditLogger(self.session)
        return self
