"""The scheduling UnitOfWork: the messaging one plus scheduled rows, users and audit, so
a scheduled delivery and the message it creates share one transaction."""

from typing import Self

from app.modules.conversations.infrastructure.repository import UserDirectory
from app.modules.messaging.infrastructure.unit_of_work import SqlMessagingUnitOfWork
from app.modules.scheduling.infrastructure.repository import ScheduledMessageRepository
from app.platform.audit import AuditLogger
from app.platform.db import SessionFactory


class SqlSchedulingUnitOfWork(SqlMessagingUnitOfWork):
    scheduled: ScheduledMessageRepository
    users: UserDirectory
    audit: AuditLogger

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self.scheduled = ScheduledMessageRepository(self.session)
        self.users = UserDirectory(self.session)
        self.audit = AuditLogger(self.session)
        return self
