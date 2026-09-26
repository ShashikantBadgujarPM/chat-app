"""The transactional-outbox EventPublisher (ADR-004, docs/design/08 §14.4).

`publish` inserts into event_outbox in the caller's transaction. The row's trigger
issues NOTIFY, which Postgres delivers only when that transaction commits: events for
rolled-back work are never sent, and nothing is sent before the data is durable.
"""

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Identity, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.events import EventPublisher as EventPublisher  # re-exported port
from app.platform.logging import get_log_context
from app.platform.models_base import Base

# Bounds how long an outbox-writing transaction can run, which the sync overlap window
# depends on (docs/design/08 §14.5, ADR-005). Applies to the rest of the transaction.
OUTBOX_STATEMENT_TIMEOUT = "5s"


class EventOutboxModel(Base):
    __tablename__ = "event_outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    recipient_user_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxEventPublisher:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish(
        self,
        event_type: str,
        *,
        recipient_user_ids: Iterable[UUID],
        payload: Mapping[str, Any],
        conversation_id: UUID | None = None,
    ) -> None:
        recipients = list(dict.fromkeys(recipient_user_ids))
        if not recipients:
            return
        # Every time, not once: a savepoint rollback (e.g. a failed scheduled item in
        # M11) would also undo an earlier SET LOCAL.
        await self._session.execute(
            text(f"SET LOCAL statement_timeout = '{OUTBOX_STATEMENT_TIMEOUT}'")
        )
        context = get_log_context()
        self._session.add(
            EventOutboxModel(
                type=event_type,
                conversation_id=conversation_id,
                recipient_user_ids=recipients,
                payload=dict(payload),
                correlation_id=context.request_id or context.batch_id,
            )
        )
        await self._session.flush()
