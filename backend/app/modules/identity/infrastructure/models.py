"""ORM model for `users` (docs/design/05 §users). Mirrors migration 0001 exactly."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.models_base import Base, TimestampMixin


class UserModel(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        # Unique only among non-deleted rows, so soft-deleting frees the name.
        Index(
            "uq_users_username_active",
            "username",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_users_email_active",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # pg_trgm has no citext operator class, so the index is on username::text
        # (Q-006). Search must query exactly `username::text`.
        Index(
            "ix_users_username_trgm",
            text("(username::text) gin_trgm_ops"),
            postgresql_using="gin",
        ),
        Index(
            "ix_users_display_name_trgm",
            "display_name",
            postgresql_using="gin",
            postgresql_ops={"display_name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    username: Mapped[str] = mapped_column(CITEXT, nullable=False)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'UTC'"))
    failed_login_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshTokenModel(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index(
            "ix_refresh_tokens_user_id_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_refresh_tokens_family_id", "family_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
