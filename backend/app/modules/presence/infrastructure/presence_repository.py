"""Persistence for presence: a cache of the connection manager's state, kept so
last_seen_at survives restarts and REST can read presence (docs/design/05 §user_presence).
Timestamps come from the database clock (Q-013)."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Text, bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.conversations.infrastructure.models import ConversationMemberModel
from app.modules.presence.domain.presence import Presence, PresenceStatus
from app.platform.models_base import Base


class UserPresenceModel(Base):
    __tablename__ = "user_presence"

    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'offline'"))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


_UPSERT = text(
    """
    INSERT INTO user_presence (user_id, status, last_seen_at, updated_at)
    VALUES (:user_id, :status, now(), now())
    ON CONFLICT (user_id) DO UPDATE
        SET status = EXCLUDED.status, last_seen_at = now(), updated_at = now()
    RETURNING user_id, status, last_seen_at
    """
)

# Everyone the user shares an active conversation with (08 §14.8).
_CO_MEMBERS = (
    select(ConversationMemberModel.user_id)
    .distinct()
    .where(
        ConversationMemberModel.left_at.is_(None),
        ConversationMemberModel.conversation_id.in_(
            select(ConversationMemberModel.conversation_id).where(
                ConversationMemberModel.user_id == bindparam("user_id"),
                ConversationMemberModel.left_at.is_(None),
            )
        ),
    )
)


class PresenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_status(self, user_id: UUID, status: PresenceStatus) -> Presence:
        row = (
            await self._session.execute(_UPSERT, {"user_id": user_id, "status": status.value})
        ).one()
        return Presence(row.user_id, PresenceStatus(row.status), row.last_seen_at)

    async def reset_all_offline(self) -> int:
        """API startup: nobody is connected to a process that just started (08 §14.7)."""
        result = await self._session.execute(
            update(UserPresenceModel)
            .where(UserPresenceModel.status == PresenceStatus.ONLINE.value)
            .values(status=PresenceStatus.OFFLINE.value, updated_at=func.now())
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def get_many(self, user_ids: Sequence[UUID]) -> dict[UUID, Presence]:
        """Presence for each id; a user with no row yet is offline, never seen."""
        found: dict[UUID, Presence] = {
            uid: Presence(uid, PresenceStatus.OFFLINE, None) for uid in user_ids
        }
        if not user_ids:
            return found
        rows = await self._session.execute(
            select(UserPresenceModel).where(UserPresenceModel.user_id.in_(user_ids))
        )
        for row in rows.scalars():
            found[row.user_id] = Presence(row.user_id, PresenceStatus(row.status), row.last_seen_at)
        return found

    async def co_member_ids(self, user_id: UUID) -> set[UUID]:
        rows = await self._session.execute(_CO_MEMBERS, {"user_id": user_id})
        return {uid for uid in rows.scalars() if uid != user_id}
