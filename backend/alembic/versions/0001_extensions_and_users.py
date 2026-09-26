"""Extensions, chat_app privileges, and the users table.

Revision ID: 0001
Revises:
Create Date: 2026-09-26

Hand-written (docs/design/12 §22.5, docs/design/05 §users, Q-004, Q-006).
Runs as chat_owner. Requires the chat_app role to exist already: the compose init
script creates it, and the test harness creates it for its own server.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ("pgcrypto", "citext", "pg_trgm")


def upgrade() -> None:
    for extension in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'chat_app') THEN
                RAISE EXCEPTION
                    'role chat_app does not exist; create it first (docs/design/12 §22.5)';
            END IF;
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO chat_app', current_database());
        END
        $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO chat_app")
    # Without FOR ROLE this applies to the current role, i.e. the owner running the
    # migrations, so it covers every table and sequence they create from here on.
    # chat_app never gets DDL rights.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO chat_app"
    )
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO chat_app")
    # Role-level (cluster-wide); bounds transaction length (docs/design/08 §14.5).
    # Skipped when already set, so migrating another database doesn't rewrite it.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole
                WHERE r.rolname = 'chat_app' AND s.setdatabase = 0
                  AND 'idle_in_transaction_session_timeout=10s' = ANY (s.setconfig)
            ) THEN
                ALTER ROLE chat_app SET idle_in_transaction_session_timeout = '10s';
            END IF;
        END
        $$;
        """
    )

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("username", postgresql.CITEXT(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("timezone", sa.Text(), server_default=sa.text("'UTC'"), nullable=False),
        sa.Column(
            "failed_login_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # op.f(): the name is final; don't run it through the naming convention again.
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_users_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        "uq_users_username_active",
        "users",
        ["username"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # pg_trgm has no citext operator class, hence the ::text expression (Q-006).
    op.execute(
        "CREATE INDEX ix_users_username_trgm ON users USING gin ((username::text) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_users_display_name_trgm ON users USING gin (display_name gin_trgm_ops)"
    )


def downgrade() -> None:
    op.drop_table("users")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE ON SEQUENCES FROM chat_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM chat_app"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM chat_app")
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM chat_app', current_database());
        END
        $$;
        """
    )
    # The role setting is cluster-wide and other databases rely on it, so it stays.
    for extension in reversed(EXTENSIONS):
        op.execute(f'DROP EXTENSION IF EXISTS "{extension}"')
