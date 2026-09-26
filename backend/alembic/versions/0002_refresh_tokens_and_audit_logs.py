"""refresh_tokens and audit_logs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26

Hand-written (docs/design/05 §refresh_tokens, §audit_logs; 12 §22.5).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name=op.f("fk_refresh_tokens_replaced_by_id_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index(
        "ix_refresh_tokens_user_id_active",
        "refresh_tokens",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_logs_actor_user_id_users"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_audit_logs_actor_user_id_created_at",
        "audit_logs",
        ["actor_user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_audit_logs_action_created_at", "audit_logs", ["action", sa.text("created_at DESC")]
    )
    # Append-only for the application role (docs/design/10 §20 "Audit", 12 §22.5).
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM chat_app")


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("refresh_tokens")
