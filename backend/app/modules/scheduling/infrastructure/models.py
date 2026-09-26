"""ORM model for scheduled messages (docs/design/05 §scheduled_messages). Mirrors
migration 0007."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.models_base import Base


class ScheduledMessageModel(Base):
    __tablename__ = "scheduled_messages"
    __table_args__ = (
        UniqueConstraint("client_message_id", name="uq_scheduled_messages_client_message_id"),
        CheckConstraint("status IN ('pending', 'sent', 'cancelled', 'failed')", name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        Index(
            "ix_scheduled_messages_status_next_attempt_at",
            "status",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_scheduled_messages_sender_id_pending",
            "sender_id",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_scheduled_messages_sender_id_scheduled_at_utc",
            "sender_id",
            "scheduled_at_utc",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    client_message_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scheduled_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sender_timezone: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    recurrence_rule: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
