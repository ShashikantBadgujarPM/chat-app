"""The identity UnitOfWork: one transaction with the identity repositories bound to it."""

from typing import Self

from app.modules.identity.infrastructure.refresh_token_repository import RefreshTokenRepository
from app.modules.identity.infrastructure.user_repository import UserRepository
from app.platform.audit import AuditLogger
from app.platform.db import SessionFactory, UnitOfWork


class SqlIdentityUnitOfWork(UnitOfWork):
    users: UserRepository
    refresh_tokens: RefreshTokenRepository
    audit: AuditLogger

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self.users = UserRepository(self.session)
        self.refresh_tokens = RefreshTokenRepository(self.session)
        self.audit = AuditLogger(self.session)
        return self
