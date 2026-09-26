"""The messaging UnitOfWork: messages plus the conversation data they depend on."""

from typing import Self

from app.modules.conversations.infrastructure.repository import ConversationRepository
from app.modules.messaging.infrastructure.repository import MessageRepository
from app.platform.db import SessionFactory, UnitOfWork
from app.platform.events import EventPublisher
from app.realtime.publisher import OutboxEventPublisher


class SqlMessagingUnitOfWork(UnitOfWork):
    messages: MessageRepository
    conversations: ConversationRepository
    events: EventPublisher

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self.messages = MessageRepository(self.session)
        self.conversations = ConversationRepository(self.session)
        self.events = OutboxEventPublisher(self.session)
        return self
