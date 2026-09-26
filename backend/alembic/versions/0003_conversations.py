"""conversations and conversation_members.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26

Hand-written (docs/design/05 §conversations, §conversation_members; Q-012).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_message_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("direct_key", sa.Text(), nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_conversations_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("type IN ('direct', 'group')", name=op.f("ck_conversations_type")),
        # A direct conversation always has a pair key; a group never does. Guards against
        # an application bug computing direct_key for the wrong type.
        sa.CheckConstraint(
            "(type = 'direct') = (direct_key IS NOT NULL)",
            name=op.f("ck_conversations_direct_key_matches_type"),
        ),
    )
    # One DM per unordered pair (R-2): INSERT … ON CONFLICT targets this index.
    op.create_index(
        "uq_conversations_direct_key",
        "conversations",
        ["direct_key"],
        unique=True,
        postgresql_where=sa.text("type = 'direct'"),
    )

    op.create_table(
        "conversation_members",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'member'"), nullable=False),
        sa.Column("last_read_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "notifications_muted", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("conversation_id", "user_id", name=op.f("pk_conversation_members")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_members_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversation_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'member')", name=op.f("ck_conversation_members_role")
        ),
    )
    # A user's active conversations: the most frequent query in the app.
    op.create_index(
        "ix_conversation_members_user_id_active",
        "conversation_members",
        ["user_id"],
        postgresql_where=sa.text("left_at IS NULL"),
    )
    # Active members of a conversation: authorization and fan-out.
    op.create_index(
        "ix_conversation_members_conversation_id_active",
        "conversation_members",
        ["conversation_id"],
        postgresql_where=sa.text("left_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("conversation_members")
    op.drop_table("conversations")
