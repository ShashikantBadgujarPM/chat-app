"""Missed-event replay (docs/design/08 §14.5, ADR-005, R-9).

Outbox ids are assigned at insert time, not commit time, so "id > cursor" can skip an
event whose transaction committed late. Instead, replay starts from a time window:
the cursor row's `created_at` (its transaction's start, never after its commit) minus
15 s. Every outbox-writing transaction is bounded (statement_timeout 5 s,
idle_in_transaction_session_timeout 10 s), so nothing can commit later than that
window covers. The overlap re-sends a few events the client already has; it dedups
them by id.

Correctness comes from REST state; replay is an optimization. So when in doubt (the
cursor was pruned, or the gap is huge), the answer is `reset_required`.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncEngine

from app.realtime.envelope import WSEvent
from app.realtime.publisher import EventOutboxModel

OVERLAP = timedelta(seconds=15)
BATCH_SIZE = 200
MAX_REPLAY_EVENTS = 5000


class ResetRequired(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ReplayPlan:
    window_start_id: int | None  # None: nothing to replay
    latest_event_id: int


def event_from_row(row: Any) -> WSEvent:
    """An event from an event_outbox row (a Core Row or an ORM instance)."""
    return WSEvent(
        id=row.id,
        type=row.type,
        conversation_id=row.conversation_id,
        occurred_at=row.created_at,
        correlation_id=row.correlation_id,
        payload=row.payload,
        recipient_user_ids=tuple(UUID(str(u)) for u in row.recipient_user_ids),
    )


class SyncService:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        # Test hook (R-16): awaited after the replay, before sync.complete is sent.
        self.before_complete: Callable[[], Awaitable[None]] | None = None

    async def latest_event_id(self) -> int:
        async with self._engine.connect() as conn:
            latest = (await conn.execute(select(func.max(EventOutboxModel.id)))).scalar()
        return int(latest or 0)

    async def replay(self, user_id: UUID, after_event_id: int) -> AsyncIterator[list[WSEvent]]:
        """Yield the user's events from the overlap window, in id order, in batches.

        An async generator: the caller streams batches to the socket as they are read,
        and each batch is a short query, so no transaction stays open for the replay.
        Raises ResetRequired before yielding anything if replay can't be trusted.
        """
        recipient = EventOutboxModel.recipient_user_ids.contains(array([user_id]))
        async with self._engine.connect() as conn:
            # Ids have gaps (rolled-back inserts consume them), so the cursor's own row
            # may not exist: anchor on the nearest row at or below it.
            anchor = (
                await conn.execute(
                    select(EventOutboxModel.created_at)
                    .where(EventOutboxModel.id <= after_event_id)
                    .order_by(EventOutboxModel.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if anchor is None:
                oldest = (await conn.execute(select(func.min(EventOutboxModel.id)))).scalar()
                if oldest is not None and after_event_id < oldest:
                    raise ResetRequired("cursor_expired")  # pruned beyond retention
                return  # an empty outbox, or a cursor from before any event
            window_start = anchor - OVERLAP
            pending = (
                await conn.execute(
                    select(func.count())
                    .select_from(EventOutboxModel)
                    .where(EventOutboxModel.created_at >= window_start, recipient)
                )
            ).scalar_one()
        if pending > MAX_REPLAY_EVENTS:
            raise ResetRequired("too_many_events")

        last_id = 0
        while True:
            async with self._engine.connect() as conn:
                rows = (
                    await conn.execute(
                        select(EventOutboxModel)
                        .where(
                            EventOutboxModel.created_at >= window_start,
                            EventOutboxModel.id > last_id,
                            recipient,
                        )
                        .order_by(EventOutboxModel.id)
                        .limit(BATCH_SIZE)
                    )
                ).all()
            if not rows:
                return
            last_id = rows[-1].id
            yield [event_from_row(row) for row in rows]
            if len(rows) < BATCH_SIZE:
                return

    async def page(
        self, user_id: UUID, after_event_id: int, limit: int
    ) -> tuple[list[WSEvent], bool]:
        """REST fallback (GET /sync/events): the same replay, one page at a time."""
        events: list[WSEvent] = []
        async for batch in self.replay(user_id, after_event_id):
            events.extend(batch)
            if len(events) > limit:
                break
        return events[:limit], len(events) > limit
