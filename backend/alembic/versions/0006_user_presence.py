"""user_presence, backfilled offline for existing users.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26

Hand-written (docs/design/05 §user_presence, M08).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_presence",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'offline'"), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_presence")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_presence_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("status IN ('online', 'offline')", name=op.f("ck_user_presence_status")),
    )
    op.execute(
        "INSERT INTO user_presence (user_id, status, last_seen_at) "
        "SELECT id, 'offline', created_at FROM users"
    )


def downgrade() -> None:
    op.drop_table("user_presence")
