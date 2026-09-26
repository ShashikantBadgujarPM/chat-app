"""Migration, privilege and constraint-registry tests against the migrated schema."""

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from alembic import command
from app.platform.db import constraint_name
from tests.factories import make_user
from tests.integration.conftest import DatabaseUnderTest, _libpq_dsn, alembic_config

# Names other code matches by (docs/design/10 §18). Extended by later milestones.
EXPECTED_CONSTRAINTS = {"pk_users", "ck_users_status"}
EXPECTED_INDEXES = {
    "uq_users_username_active",
    "uq_users_email_active",
    "ix_users_username_trgm",
    "ix_users_display_name_trgm",
}


async def test_expected_constraints_exist(session: AsyncSession) -> None:
    rows = await session.execute(
        text("SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace")
    )
    assert set(rows.scalars()) >= EXPECTED_CONSTRAINTS


async def test_expected_indexes_exist(session: AsyncSession) -> None:
    rows = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    )
    assert set(rows.scalars()) >= EXPECTED_INDEXES


async def test_extensions_are_installed(session: AsyncSession) -> None:
    rows = await session.execute(text("SELECT extname FROM pg_extension"))
    assert {"pgcrypto", "citext", "pg_trgm"} <= set(rows.scalars())


async def test_app_role_is_chat_app(session: AsyncSession) -> None:
    assert (await session.execute(text("SELECT current_user"))).scalar_one() == "chat_app"


async def test_app_role_cannot_run_ddl(session: AsyncSession) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        await session.execute(text("CREATE TABLE not_allowed (id int)"))


async def test_app_role_can_read_and_write_users(session: AsyncSession) -> None:
    user = await make_user(session, username="dml_check")
    await session.execute(
        text("UPDATE users SET display_name = 'Changed' WHERE id = :id"), {"id": user.id}
    )
    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})


async def test_app_role_has_the_idle_transaction_timeout(session: AsyncSession) -> None:
    value = (await session.execute(text("SHOW idle_in_transaction_session_timeout"))).scalar_one()
    assert value == "10s"


async def test_status_check_constraint(session: AsyncSession) -> None:
    await make_user(session, username="bad_status")
    with pytest.raises(IntegrityError) as caught:
        await session.execute(text("UPDATE users SET status = 'banned'"))
    assert constraint_name(caught.value) == "ck_users_status"


async def test_username_trigram_index_is_used(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        (
            await session.execute(
                text("EXPLAIN SELECT id FROM users WHERE username::text ILIKE '%ali%'")
            )
        ).scalars()
    )
    assert "ix_users_username_trgm" in plan


def test_alembic_upgrade_downgrade_upgrade_round_trip(test_database: DatabaseUnderTest) -> None:
    config = alembic_config(test_database.owner_url)

    command.downgrade(config, "base")
    with psycopg.connect(_libpq_dsn(test_database.owner_url)) as conn:
        remaining = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename <> "
            "'alembic_version'"
        ).fetchone()
    assert remaining == (0,)

    command.upgrade(config, "head")


async def test_engine_connects_as_the_app_role(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        assert (await conn.execute(text("SELECT current_user"))).scalar_one() == "chat_app"


async def test_dbapi_errors_expose_constraint_names(session: AsyncSession) -> None:
    await make_user(session, username="dupe")
    with pytest.raises(DBAPIError) as caught:
        await make_user(session, username="DUPE")
    assert constraint_name(caught.value) == "uq_users_username_active"
