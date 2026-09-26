"""Persistence for scheduled messages. Every state change is one conditional statement,
so the database resolves races with the worker (docs/design/09 §16.4); no commits here."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.messaging.infrastructure.models import MessageModel
from app.modules.scheduling.domain.scheduled_message import (
    ScheduledMessage,
    ScheduledStatus,
)
from app.modules.scheduling.infrastructure.models import ScheduledMessageModel

Model = ScheduledMessageModel

# SKIP LOCKED: concurrent workers skip rows another transaction holds instead of
# waiting on them, so N workers never claim the same row (09 §16.1). The row lock is
# the only "in progress" marker; a crashed worker's rollback releases it.
_CLAIM_DUE = text(
    """
    SELECT scheduled_messages.*, now() AS db_now
    FROM scheduled_messages
    WHERE status = 'pending' AND next_attempt_at <= now()
    ORDER BY next_attempt_at
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
    """
)


def _scheduled(row: Any) -> ScheduledMessage:
    return ScheduledMessage(
        id=row.id,
        conversation_id=row.conversation_id,
        sender_id=row.sender_id,
        body=row.body,
        reply_to_id=row.reply_to_id,
        client_message_id=row.client_message_id,
        scheduled_at_utc=row.scheduled_at_utc,
        sender_timezone=row.sender_timezone,
        status=ScheduledStatus(row.status),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        sent_at=row.sent_at,
        cancelled_at=row.cancelled_at,
    )


@dataclass(frozen=True, slots=True)
class SchedulerStats:
    pending_total: int
    due_now: int
    oldest_due_lag_s: float


class ScheduledMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _update_returning(self, statement: Any) -> ScheduledMessage | None:
        row = (await self._session.execute(statement.returning(Model))).scalar_one_or_none()
        return _scheduled(row) if row is not None else None

    # --- API side --------------------------------------------------------------------

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
    ) -> ScheduledMessage | None:
        """Insert, or None if this client_message_id already exists (a double submit)."""
        statement = (
            insert(Model)
            .values(
                conversation_id=conversation_id,
                sender_id=sender_id,
                body=body,
                reply_to_id=reply_to_id,
                client_message_id=client_message_id,
                scheduled_at_utc=scheduled_at_utc,
                next_attempt_at=scheduled_at_utc,
                sender_timezone=sender_timezone,
            )
            .on_conflict_do_nothing(constraint="uq_scheduled_messages_client_message_id")
            .returning(Model)
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _scheduled(row) if row is not None else None

    async def get(self, scheduled_id: UUID) -> ScheduledMessage | None:
        row = await self._session.get(Model, scheduled_id, populate_existing=True)
        return _scheduled(row) if row is not None else None

    async def get_by_client_id(self, client_message_id: UUID) -> ScheduledMessage | None:
        row = (
            await self._session.execute(
                select(Model).where(Model.client_message_id == client_message_id)
            )
        ).scalar_one_or_none()
        return _scheduled(row) if row is not None else None

    async def count_pending(self, sender_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count()).where(Model.sender_id == sender_id, Model.status == "pending")
        )
        return int(result.scalar_one())

    async def list_for_sender(
        self,
        sender_id: UUID,
        *,
        status: ScheduledStatus | None,
        conversation_id: UUID | None,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[ScheduledMessage]:
        """Ordered by (scheduled_at_utc, id), keyset-paginated from `after`."""
        statement = (
            select(Model)
            .where(Model.sender_id == sender_id)
            .order_by(Model.scheduled_at_utc, Model.id)
            .limit(limit)
        )
        if status is not None:
            statement = statement.where(Model.status == status.value)
        if conversation_id is not None:
            statement = statement.where(Model.conversation_id == conversation_id)
        if after is not None:
            statement = statement.where(tuple_(Model.scheduled_at_utc, Model.id) > after)
        return [_scheduled(r) for r in (await self._session.execute(statement)).scalars()]

    async def update_pending(
        self, scheduled_id: UUID, sender_id: UUID, changes: Mapping[str, Any]
    ) -> ScheduledMessage | None:
        """Edit, only while pending. If a worker holds the row, this blocks until it
        commits; READ COMMITTED then re-checks the WHERE against the new version and
        matches nothing if it was sent (09 §16.4)."""
        return await self._update_returning(
            update(Model)
            .where(
                Model.id == scheduled_id, Model.sender_id == sender_id, Model.status == "pending"
            )
            .values(**changes, updated_at=func.now())
        )

    async def cancel(self, scheduled_id: UUID, sender_id: UUID) -> ScheduledMessage | None:
        return await self._update_returning(
            update(Model)
            .where(
                Model.id == scheduled_id, Model.sender_id == sender_id, Model.status == "pending"
            )
            .values(status="cancelled", cancelled_at=func.now(), updated_at=func.now())
        )

    async def reset_failed(
        self, scheduled_id: UUID, sender_id: UUID, *, scheduled_at_utc: datetime
    ) -> ScheduledMessage | None:
        """Retry: failed -> pending, with a fresh attempt budget."""
        return await self._update_returning(
            update(Model)
            .where(Model.id == scheduled_id, Model.sender_id == sender_id, Model.status == "failed")
            .values(
                status="pending",
                attempts=0,
                last_error=None,
                scheduled_at_utc=scheduled_at_utc,
                next_attempt_at=scheduled_at_utc,
                updated_at=func.now(),
            )
        )

    async def sent_message_ids(self, scheduled_ids: Sequence[UUID]) -> dict[UUID, UUID]:
        """The message each scheduled row produced, in one query."""
        if not scheduled_ids:
            return {}
        rows = await self._session.execute(
            select(MessageModel.scheduled_message_id, MessageModel.id).where(
                MessageModel.scheduled_message_id.in_(scheduled_ids)
            )
        )
        return {sid: mid for sid, mid in rows.all() if sid is not None}

    # --- worker side -----------------------------------------------------------------

    async def claim_due(self, batch_size: int) -> tuple[datetime | None, list[ScheduledMessage]]:
        """Lock up to `batch_size` due rows for this transaction. Also returns the
        database's now() (the transaction start), which lateness is measured against."""
        rows = (await self._session.execute(_CLAIM_DUE, {"batch_size": batch_size})).all()
        if not rows:
            return None, []
        return rows[0].db_now, [_scheduled(r) for r in rows]

    async def mark_sent(self, scheduled_id: UUID) -> ScheduledMessage:
        row = await self._update_returning(
            update(Model)
            .where(Model.id == scheduled_id)
            .values(
                status="sent",
                attempts=Model.attempts + 1,
                sent_at=func.now(),
                last_error=None,
                updated_at=func.now(),
            )
        )
        assert row is not None  # noqa: S101 - the caller holds the row lock
        return row

    async def mark_retry(
        self, scheduled_id: UUID, *, next_attempt_at: datetime, error: str
    ) -> ScheduledMessage:
        row = await self._update_returning(
            update(Model)
            .where(Model.id == scheduled_id)
            .values(
                attempts=Model.attempts + 1,
                next_attempt_at=next_attempt_at,
                last_error=error,
                updated_at=func.now(),
            )
        )
        assert row is not None  # noqa: S101 - the caller holds the row lock
        return row

    async def mark_failed(
        self, scheduled_id: UUID, *, reason: str, count_attempt: bool
    ) -> ScheduledMessage:
        row = await self._update_returning(
            update(Model)
            .where(Model.id == scheduled_id)
            .values(
                status="failed",
                attempts=Model.attempts + 1 if count_attempt else Model.attempts,
                last_error=reason,
                updated_at=func.now(),
            )
        )
        assert row is not None  # noqa: S101 - the caller holds the row lock
        return row

    async def stats(self) -> SchedulerStats:
        """Is the scheduler keeping up? (09 §17.5)"""
        row = (
            await self._session.execute(
                text(
                    """
                    SELECT count(*) AS pending_total,
                           count(*) FILTER (WHERE next_attempt_at <= now()) AS due_now,
                           coalesce(extract(epoch FROM now() - min(next_attempt_at)
                               FILTER (WHERE next_attempt_at <= now())), 0) AS oldest_due_lag_s
                    FROM scheduled_messages
                    WHERE status = 'pending'
                    """
                )
            )
        ).one()
        return SchedulerStats(
            pending_total=int(row.pending_total),
            due_now=int(row.due_now),
            oldest_due_lag_s=float(row.oldest_due_lag_s),
        )
