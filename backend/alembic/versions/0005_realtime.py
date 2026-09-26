"""event_outbox with its NOTIFY trigger, and ws_tickets.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26

Hand-written (docs/design/05 §event_outbox, §ws_tickets; 08 §14.4; ADR-004; Q-014).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_outbox",
        # The global event id: the client's resume cursor (last_event_id).
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Frozen at write time from membership, so replay never re-evaluates it.
        sa.Column(
            "recipient_user_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        # The request (or worker job) that caused the event (docs/design/10 §19.2).
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_outbox")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_event_outbox_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_event_outbox_recipient_user_ids",
        "event_outbox",
        ["recipient_user_ids"],
        postgresql_using="gin",
    )
    op.create_index("ix_event_outbox_created_at", "event_outbox", ["created_at"])

    # Postgres delivers NOTIFY only at commit, in commit order, and never for rolled-
    # back work (ADR-004). The payload is just the id: NOTIFY payloads are capped at
    # about 8000 bytes, so the listener re-reads the row.
    op.execute(
        """
        CREATE FUNCTION notify_event_outbox() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('events', NEW.id::text);
            RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER event_outbox_notify
        AFTER INSERT ON event_outbox
        FOR EACH ROW EXECUTE FUNCTION notify_event_outbox();
        """
    )

    op.create_table(
        "ws_tickets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ws_tickets")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_ws_tickets_user_id_users"), ondelete="CASCADE"
        ),
        sa.UniqueConstraint("ticket_hash", name=op.f("uq_ws_tickets_ticket_hash")),
    )
    op.create_index("ix_ws_tickets_expires_at", "ws_tickets", ["expires_at"])


def downgrade() -> None:
    op.drop_table("ws_tickets")
    op.execute("DROP TRIGGER IF EXISTS event_outbox_notify ON event_outbox")
    op.execute("DROP FUNCTION IF EXISTS notify_event_outbox()")
    op.drop_table("event_outbox")
