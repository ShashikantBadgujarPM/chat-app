"""Persistence for conversations and memberships.

Race guards are SQL, not Python (docs/design/15 §26.4 #6, 13 §24): DM creation uses
ON CONFLICT (R-2), owner removal locks the owner rows (R-12), and adding members is an
upsert (R-14). Repositories never commit.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.conversations.domain.model import (
    Conversation,
    ConversationType,
    MemberProfile,
    MemberRole,
    Membership,
)
from app.modules.conversations.infrastructure.models import (
    ConversationMemberModel,
    ConversationModel,
)
from app.modules.identity.infrastructure.models import UserModel


def _conversation(row: Any) -> Conversation:
    return Conversation(
        id=row.id,
        type=ConversationType(row.type),
        title=row.title,
        created_by=row.created_by,
        last_message_seq=row.last_message_seq,
        last_activity_at=row.last_activity_at,
        created_at=row.created_at,
    )


def _membership(row: Any) -> Membership:
    return Membership(
        conversation_id=row.conversation_id,
        user_id=row.user_id,
        role=MemberRole(row.role),
        last_read_seq=row.last_read_seq,
        notifications_muted=row.notifications_muted,
        joined_at=row.joined_at,
        left_at=row.left_at,
    )


def _profile(row: Any) -> MemberProfile:
    return MemberProfile(
        user_id=row.user_id,
        username=row.username,
        display_name=row.display_name,
        role=MemberRole(row.role),
        joined_at=row.joined_at,
        notifications_muted=row.notifications_muted,
    )


# R-2: one insert wins; a concurrent loser gets no row back and reads the winner.
_INSERT_DIRECT = text(
    """
    INSERT INTO conversations (type, direct_key, created_by)
    VALUES ('direct', :direct_key, :created_by)
    ON CONFLICT (direct_key) WHERE type = 'direct' DO NOTHING
    RETURNING id
    """
)

# R-14: idempotent add. A former member is reactivated with their read cursor at the
# current end of the conversation, so they don't return to a huge unread count. An
# active member is left untouched and no row is returned.
_UPSERT_MEMBER = text(
    """
    INSERT INTO conversation_members (conversation_id, user_id, role, last_read_seq)
    SELECT c.id, :user_id, :role, c.last_message_seq
    FROM conversations AS c
    WHERE c.id = :conversation_id
    ON CONFLICT (conversation_id, user_id) DO UPDATE
        SET left_at = NULL,
            joined_at = now(),
            role = EXCLUDED.role,
            last_read_seq = EXCLUDED.last_read_seq,
            notifications_muted = false
        WHERE conversation_members.left_at IS NOT NULL
    RETURNING *
    """
)


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- conversations ---------------------------------------------------------------

    async def insert_direct_on_conflict(
        self, *, direct_key: str, created_by: UUID
    ) -> tuple[UUID, bool]:
        """Returns (conversation id, created). Race-safe (R-2)."""
        inserted = (
            await self._session.execute(
                _INSERT_DIRECT, {"direct_key": direct_key, "created_by": created_by}
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return inserted, True
        existing = (
            await self._session.execute(
                select(ConversationModel.id).where(
                    ConversationModel.type == ConversationType.DIRECT.value,
                    ConversationModel.direct_key == direct_key,
                )
            )
        ).scalar_one()
        return existing, False

    async def create_group(self, *, title: str, created_by: UUID) -> UUID:
        row = ConversationModel(
            type=ConversationType.GROUP.value, title=title, created_by=created_by
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def get(self, conversation_id: UUID, *, for_update: bool = False) -> Conversation | None:
        statement = select(ConversationModel).where(ConversationModel.id == conversation_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _conversation(row) if row is not None else None

    async def rename(self, conversation_id: UUID, title: str) -> None:
        await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(title=title)
        )

    async def list_for_user(
        self, user_id: UUID, *, after: tuple[datetime, UUID] | None, limit: int
    ) -> list[tuple[Conversation, Membership]]:
        """The user's active conversations, most recent activity first (Q-012)."""
        statement = (
            select(ConversationModel, ConversationMemberModel)
            .join(
                ConversationMemberModel,
                ConversationMemberModel.conversation_id == ConversationModel.id,
            )
            .where(
                ConversationMemberModel.user_id == user_id,
                ConversationMemberModel.left_at.is_(None),
            )
            .order_by(ConversationModel.last_activity_at.desc(), ConversationModel.id.asc())
            .limit(limit)
        )
        if after is not None:
            at, after_id = after
            statement = statement.where(
                (ConversationModel.last_activity_at < at)
                | ((ConversationModel.last_activity_at == at) & (ConversationModel.id > after_id))
            )
        rows = (await self._session.execute(statement)).all()
        return [(_conversation(c), _membership(m)) for c, m in rows]

    # --- memberships -----------------------------------------------------------------

    async def get_membership(self, conversation_id: UUID, user_id: UUID) -> Membership | None:
        """The membership row, active or not."""
        row = await self._session.get(ConversationMemberModel, (conversation_id, user_id))
        return _membership(row) if row is not None else None

    async def get_active_membership(
        self, conversation_id: UUID, user_id: UUID
    ) -> Membership | None:
        membership = await self.get_membership(conversation_id, user_id)
        return membership if membership is not None and membership.is_active else None

    async def add_member(self, conversation_id: UUID, user_id: UUID, role: MemberRole) -> None:
        """Plain insert, for members of a conversation created in this transaction."""
        self._session.add(
            ConversationMemberModel(
                conversation_id=conversation_id, user_id=user_id, role=role.value
            )
        )
        await self._session.flush()

    async def upsert_member(
        self, conversation_id: UUID, user_id: UUID, role: MemberRole = MemberRole.MEMBER
    ) -> Membership | None:
        """Add or reactivate (R-14). None if the user already is an active member."""
        row = (
            await self._session.execute(
                _UPSERT_MEMBER,
                {"conversation_id": conversation_id, "user_id": user_id, "role": role.value},
            )
        ).one_or_none()
        return _membership(row) if row is not None else None

    async def lock_owner_rows(self, conversation_id: UUID) -> list[UUID]:
        """Lock the active owners' rows (R-12) and return their user ids.

        Concurrent owner removals take these same locks, so they run one after the
        other and the second one sees the first one's result.
        """
        rows = await self._session.execute(
            select(ConversationMemberModel.user_id)
            .where(
                ConversationMemberModel.conversation_id == conversation_id,
                ConversationMemberModel.role == MemberRole.OWNER.value,
                ConversationMemberModel.left_at.is_(None),
            )
            .order_by(ConversationMemberModel.user_id)  # a consistent lock order
            .with_for_update()
        )
        return list(rows.scalars())

    async def mark_left(self, conversation_id: UUID, user_id: UUID, *, now: datetime) -> bool:
        result = await self._session.execute(
            update(ConversationMemberModel)
            .where(
                ConversationMemberModel.conversation_id == conversation_id,
                ConversationMemberModel.user_id == user_id,
                ConversationMemberModel.left_at.is_(None),
            )
            .values(left_at=now)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def set_muted(self, conversation_id: UUID, user_id: UUID, muted: bool) -> None:
        await self._session.execute(
            update(ConversationMemberModel)
            .where(
                ConversationMemberModel.conversation_id == conversation_id,
                ConversationMemberModel.user_id == user_id,
            )
            .values(notifications_muted=muted)
        )

    async def count_active_members(self, conversation_id: UUID) -> int:
        count = await self._session.execute(
            select(func.count()).where(
                ConversationMemberModel.conversation_id == conversation_id,
                ConversationMemberModel.left_at.is_(None),
            )
        )
        return int(count.scalar_one())

    async def active_member_ids(self, conversation_id: UUID) -> list[UUID]:
        rows = await self._session.execute(
            select(ConversationMemberModel.user_id).where(
                ConversationMemberModel.conversation_id == conversation_id,
                ConversationMemberModel.left_at.is_(None),
            )
        )
        return list(rows.scalars())

    async def list_members(
        self, conversation_id: UUID, *, user_ids: Sequence[UUID] | None = None
    ) -> list[MemberProfile]:
        statement = (
            select(
                ConversationMemberModel.user_id,
                UserModel.username,
                UserModel.display_name,
                ConversationMemberModel.role,
                ConversationMemberModel.joined_at,
                ConversationMemberModel.notifications_muted,
            )
            .join(UserModel, UserModel.id == ConversationMemberModel.user_id)
            .where(
                ConversationMemberModel.conversation_id == conversation_id,
                ConversationMemberModel.left_at.is_(None),
            )
            .order_by(ConversationMemberModel.joined_at, ConversationMemberModel.user_id)
        )
        if user_ids is not None:
            statement = statement.where(ConversationMemberModel.user_id.in_(user_ids))
        return [_profile(row) for row in (await self._session.execute(statement)).all()]

    async def member_summaries(
        self, conversation_ids: Sequence[UUID], *, viewer_id: UUID, preview_size: int
    ) -> dict[UUID, tuple[int, list[MemberProfile]]]:
        """Per conversation: (active member count, up to `preview_size` members, others
        before the viewer). One query for a whole page of conversations, not N."""
        if not conversation_ids:
            return {}
        ranked = (
            select(
                ConversationMemberModel.conversation_id,
                ConversationMemberModel.user_id,
                UserModel.username,
                UserModel.display_name,
                ConversationMemberModel.role,
                ConversationMemberModel.joined_at,
                ConversationMemberModel.notifications_muted,
                func.count()
                .over(partition_by=ConversationMemberModel.conversation_id)
                .label("member_count"),
                func.row_number()
                .over(
                    partition_by=ConversationMemberModel.conversation_id,
                    order_by=(
                        (ConversationMemberModel.user_id == viewer_id).asc(),
                        ConversationMemberModel.joined_at,
                        ConversationMemberModel.user_id,
                    ),
                )
                .label("position"),
            )
            .join(UserModel, UserModel.id == ConversationMemberModel.user_id)
            .where(
                ConversationMemberModel.conversation_id.in_(conversation_ids),
                ConversationMemberModel.left_at.is_(None),
            )
            .subquery()
        )
        rows = (
            await self._session.execute(
                select(ranked)
                .where(ranked.c.position <= preview_size)
                .order_by(ranked.c.conversation_id, ranked.c.position)
            )
        ).all()
        summaries: dict[UUID, tuple[int, list[MemberProfile]]] = {
            cid: (0, []) for cid in conversation_ids
        }
        for row in rows:
            _, preview = summaries[row.conversation_id]
            preview.append(_profile(row))
            summaries[row.conversation_id] = (int(row.member_count), preview)
        return summaries


class UserDirectory:
    """Read access to users for this module: which ids are active accounts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_user_ids(self, user_ids: Sequence[UUID]) -> set[UUID]:
        if not user_ids:
            return set()
        rows = await self._session.execute(
            select(UserModel.id).where(
                UserModel.id.in_(user_ids),
                UserModel.deleted_at.is_(None),
                UserModel.status == "active",
            )
        )
        return set(rows.scalars())
