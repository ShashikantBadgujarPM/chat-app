"""SQL adapter for the PresenceStore port: one short transaction per call."""

from collections.abc import Sequence
from uuid import UUID

from app.modules.presence.domain.presence import Presence, PresenceStatus
from app.modules.presence.infrastructure.presence_repository import PresenceRepository
from app.platform.db import SessionFactory, UnitOfWork


class SqlPresenceStore:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def upsert_status(self, user_id: UUID, status: PresenceStatus) -> Presence:
        async with UnitOfWork(self._session_factory) as uow:
            return await PresenceRepository(uow.session).upsert_status(user_id, status)

    async def get_many(self, user_ids: Sequence[UUID]) -> dict[UUID, Presence]:
        async with UnitOfWork(self._session_factory) as uow:
            return await PresenceRepository(uow.session).get_many(user_ids)

    async def co_member_ids(self, user_id: UUID) -> set[UUID]:
        async with UnitOfWork(self._session_factory) as uow:
            return await PresenceRepository(uow.session).co_member_ids(user_id)

    async def reset_all_offline(self) -> int:
        async with UnitOfWork(self._session_factory) as uow:
            return await PresenceRepository(uow.session).reset_all_offline()
