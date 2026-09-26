"""scheduled_messages, and the messages.scheduled_message_id link.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26

Hand-written (docs/design/05 §scheduled_messages, 09 §16-17, M11). The two tables
reference each other (messages.scheduled_message_id and scheduled_messages.reply_to_id),
so the table is created first and both foreign keys are added afterwards.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reply_to_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Reused as the message's client_message_id at send time: idempotency from
        # scheduling through delivery.
        sa.Column("client_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scheduled_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sender_timezone", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        # Set by the application to scheduled_at_utc (a default can't reference another
        # column); pushed back by retry backoff. Drives claim order.
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        # Reserved for recurring messages (09 §17.7); always NULL in v1.
        sa.Column("recurrence_rule", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_messages")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_scheduled_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"],
            ["users.id"],
            name=op.f("fk_scheduled_messages_sender_id_users"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "client_message_id", name=op.f("uq_scheduled_messages_client_message_id")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'cancelled', 'failed')",
            name=op.f("ck_scheduled_messages_status"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_scheduled_messages_attempts")),
    )
    # The scheduler's claim query: WHERE status = 'pending' AND next_attempt_at <= now()
    # ORDER BY next_attempt_at. Partial, so it stays tiny however much history piles up.
    op.create_index(
        "ix_scheduled_messages_status_next_attempt_at",
        "scheduled_messages",
        ["status", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # "My pending scheduled messages" and the per-user pending limit.
    op.create_index(
        "ix_scheduled_messages_sender_id_pending",
        "scheduled_messages",
        ["sender_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # The scheduled list (all statuses) is keyset-paginated by (scheduled_at_utc, id).
    op.create_index(
        "ix_scheduled_messages_sender_id_scheduled_at_utc",
        "scheduled_messages",
        ["sender_id", "scheduled_at_utc", "id"],
    )

    # The hard guard against a scheduled message being materialized twice (R-3).
    op.create_foreign_key(
        op.f("fk_messages_scheduled_message_id_scheduled_messages"),
        "messages",
        "scheduled_messages",
        ["scheduled_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        op.f("uq_messages_scheduled_message_id"), "messages", ["scheduled_message_id"]
    )
    # Added last: this FK and the one above form a cycle.
    op.create_foreign_key(
        op.f("fk_scheduled_messages_reply_to_id_messages"),
        "scheduled_messages",
        "messages",
        ["reply_to_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_scheduled_messages_reply_to_id_messages"), "scheduled_messages", type_="foreignkey"
    )
    op.drop_constraint(op.f("uq_messages_scheduled_message_id"), "messages", type_="unique")
    op.drop_constraint(
        op.f("fk_messages_scheduled_message_id_scheduled_messages"), "messages", type_="foreignkey"
    )
    op.drop_table("scheduled_messages")
