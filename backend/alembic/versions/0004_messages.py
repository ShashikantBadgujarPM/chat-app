"""messages.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26

Hand-written (docs/design/05 §messages, M05). `scheduled_message_id` is a plain
nullable column for now: M11 adds its FK and unique constraint once
scheduled_messages exists. `search_vector` and its GIN index come in M12.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("reply_to_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("client_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scheduled_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"],
            ["users.id"],
            name=op.f("fk_messages_sender_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reply_to_id"],
            ["messages.id"],
            name=op.f("fk_messages_reply_to_id_messages"),
            ondelete="SET NULL",
        ),
        # The ordering contract (R-1).
        sa.UniqueConstraint("conversation_id", "seq", name=op.f("uq_messages_conversation_id_seq")),
        # Idempotent send (R-5).
        sa.UniqueConstraint(
            "sender_id", "client_message_id", name=op.f("uq_messages_sender_id_client_message_id")
        ),
        # A tombstone has no body, and only a tombstone.
        sa.CheckConstraint(
            "(deleted_at IS NULL) = (body IS NOT NULL)", name=op.f("ck_messages_body_or_deleted")
        ),
    )
    # History pagination and unread counting: WHERE conversation_id = ? AND seq < ?
    # ORDER BY seq DESC. (The unique constraint's index is ASC; this one serves the
    # descending scans directly.)
    op.create_index(
        "ix_messages_conversation_id_seq_desc",
        "messages",
        ["conversation_id", sa.text("seq DESC")],
    )
    op.create_index(
        "ix_messages_reply_to_id",
        "messages",
        ["reply_to_id"],
        postgresql_where=sa.text("reply_to_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("messages")
