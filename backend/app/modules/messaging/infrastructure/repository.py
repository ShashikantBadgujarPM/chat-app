"""Persistence for messages. Race guards are SQL (R-1, R-5, R-13); no commits here."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.conversations.infrastructure.models import ConversationModel
from app.modules.identity.infrastructure.models import UserModel
from app.modules.messaging.domain.message import Message, MessageView, SenderProfile
from app.modules.messaging.infrastructure.models import MessageModel


def _message(row: Any) -> Message:
    return Message(
        id=row.id,
        conversation_id=row.conversation_id,
        seq=row.seq,
        sender_id=row.sender_id,
        body=row.body,
        reply_to_id=row.reply_to_id,
        client_message_id=row.client_message_id,
        scheduled_message_id=row.scheduled_message_id,
        created_at=row.created_at,
        edited_at=row.edited_at,
        deleted_at=row.deleted_at,
    )


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- writes ----------------------------------------------------------------------

    async def next_seq(self, conversation_id: UUID) -> int:
        """Take the next seq (R-1). The UPDATE's row lock serializes sends to *this*
        conversation only; it also records the activity for the conversation list.

        Row timestamps come from the database clock, like every created_at default, so
        they are consistent with each other; the Clock port is for domain decisions."""
        result = await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(
                last_message_seq=ConversationModel.last_message_seq + 1,
                last_activity_at=func.now(),
            )
            .returning(ConversationModel.last_message_seq)
        )
        return int(result.scalar_one())

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
    ) -> Message | None:
        """Insert, or return None if this (sender, client_message_id) already exists (R-5)."""
        statement = (
            insert(MessageModel)
            .values(
                conversation_id=conversation_id,
                seq=seq,
                sender_id=sender_id,
                body=body,
                reply_to_id=reply_to_id,
                client_message_id=client_message_id,
                scheduled_message_id=scheduled_message_id,
            )
            .on_conflict_do_nothing(constraint="uq_messages_sender_id_client_message_id")
            .returning(MessageModel)
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _message(row) if row is not None else None

    async def edit(self, message_id: UUID, *, sender_id: UUID, body: str) -> Message | None:
        """R-13: only the sender's, not-yet-deleted message. None if nothing matched."""
        row = (
            await self._session.execute(
                update(MessageModel)
                .where(
                    MessageModel.id == message_id,
                    MessageModel.sender_id == sender_id,
                    MessageModel.deleted_at.is_(None),
                )
                .values(body=body, edited_at=func.now())
                .returning(MessageModel)
            )
        ).scalar_one_or_none()
        return _message(row) if row is not None else None

    async def soft_delete(self, message_id: UUID, *, sender_id: UUID) -> Message | None:
        """Tombstone: body nulled, deleted_at set. None if already deleted (or not theirs)."""
        row = (
            await self._session.execute(
                update(MessageModel)
                .where(
                    MessageModel.id == message_id,
                    MessageModel.sender_id == sender_id,
                    MessageModel.deleted_at.is_(None),
                )
                .values(body=None, deleted_at=func.now())
                .returning(MessageModel)
            )
        ).scalar_one_or_none()
        return _message(row) if row is not None else None

    # --- reads -----------------------------------------------------------------------

    async def get(self, message_id: UUID) -> Message | None:
        row = await self._session.get(MessageModel, message_id, populate_existing=True)
        return _message(row) if row is not None else None

    async def get_by_client_id(self, sender_id: UUID, client_message_id: UUID) -> Message | None:
        row = (
            await self._session.execute(
                select(MessageModel).where(
                    MessageModel.sender_id == sender_id,
                    MessageModel.client_message_id == client_message_id,
                )
            )
        ).scalar_one_or_none()
        return _message(row) if row is not None else None

    async def page_before(
        self, conversation_id: UUID, *, before_seq: int | None, limit: int
    ) -> list[Message]:
        """Newest first. `before_seq=None` is the latest page."""
        statement = (
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id)
            .order_by(MessageModel.seq.desc())
            .limit(limit)
        )
        if before_seq is not None:
            statement = statement.where(MessageModel.seq < before_seq)
        return [_message(r) for r in (await self._session.execute(statement)).scalars()]

    async def page_after(
        self, conversation_id: UUID, *, after_seq: int, limit: int
    ) -> list[Message]:
        """Oldest first."""
        statement = (
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id, MessageModel.seq > after_seq)
            .order_by(MessageModel.seq.asc())
            .limit(limit)
        )
        return [_message(r) for r in (await self._session.execute(statement)).scalars()]

    async def latest_by_conversation(
        self, pairs: Sequence[tuple[UUID, int]]
    ) -> dict[UUID, Message]:
        """The message at each conversation's `last_message_seq`, in one query."""
        wanted = [(cid, seq) for cid, seq in pairs if seq > 0]
        if not wanted:
            return {}
        rows = await self._session.execute(
            select(MessageModel).where(
                tuple_(MessageModel.conversation_id, MessageModel.seq).in_(wanted)
            )
        )
        return {m.conversation_id: _message(m) for m in rows.scalars()}

    async def load_views(self, messages: Sequence[Message]) -> list[MessageView]:
        """Attach senders and reply targets with two batched queries (no N+1)."""
        sender_ids = {m.sender_id for m in messages if m.sender_id is not None}
        reply_ids = {m.reply_to_id for m in messages if m.reply_to_id is not None}
        senders: dict[UUID, SenderProfile] = {}
        if sender_ids:
            rows = await self._session.execute(
                select(UserModel.id, UserModel.username, UserModel.display_name).where(
                    UserModel.id.in_(sender_ids)
                )
            )
            senders = {r.id: SenderProfile(r.id, r.username, r.display_name) for r in rows}
        replies: dict[UUID, Message] = {}
        if reply_ids:
            reply_rows = await self._session.execute(
                select(MessageModel).where(MessageModel.id.in_(reply_ids))
            )
            replies = {m.id: _message(m) for m in reply_rows.scalars()}
        return [
            MessageView(
                message=m,
                sender=senders.get(m.sender_id) if m.sender_id else None,
                reply_to=replies.get(m.reply_to_id) if m.reply_to_id else None,
                mentions=[],  # parsed and stored from M10
            )
            for m in messages
        ]
