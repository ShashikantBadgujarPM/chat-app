"""ORM model for messages (docs/design/05 §messages). Mirrors migration 0004."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.models_base import Base


class MessageModel(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_id_seq"),
        UniqueConstraint(
            "sender_id", "client_message_id", name="uq_messages_sender_id_client_message_id"
        ),
        UniqueConstraint("scheduled_message_id", name="uq_messages_scheduled_message_id"),
        CheckConstraint("(deleted_at IS NULL) = (body IS NOT NULL)", name="body_or_deleted"),
        Index("ix_messages_conversation_id_seq_desc", "conversation_id", text("seq DESC")),
        Index(
            "ix_messages_reply_to_id",
            "reply_to_id",
            postgresql_where=text("reply_to_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    body: Mapped[str | None] = mapped_column(Text)
    reply_to_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    client_message_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # FK to scheduled_messages (migration 0007). Not declared here: the two tables
    # reference each other, and the ORM never needs to follow this link.
    scheduled_message_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
