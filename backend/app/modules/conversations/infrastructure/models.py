"""ORM models for conversations (docs/design/05). Mirror migration 0003."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.models_base import Base, TimestampMixin


class ConversationModel(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("type IN ('direct', 'group')", name="type"),
        CheckConstraint(
            "(type = 'direct') = (direct_key IS NOT NULL)", name="direct_key_matches_type"
        ),
        Index(
            "uq_conversations_direct_key",
            "direct_key",
            unique=True,
            postgresql_where=text("type = 'direct'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    last_message_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    direct_key: Mapped[str | None] = mapped_column(Text)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConversationMemberModel(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'member')", name="role"),
        Index(
            "ix_conversation_members_user_id_active",
            "user_id",
            postgresql_where=text("left_at IS NULL"),
        ),
        Index(
            "ix_conversation_members_conversation_id_active",
            "conversation_id",
            postgresql_where=text("left_at IS NULL"),
        ),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'member'"))
    last_read_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    notifications_muted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
