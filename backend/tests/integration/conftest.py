"""Real-PostgreSQL test harness (docs/design/11 §21.3, Q-005).

Server: TEST_DATABASE_ADMIN_URL (a superuser URL) when set, as in CI or inside the api
container; otherwise one testcontainer per test process.

Per xdist worker: database `test_<worker_id>`, migrated with Alembic as the owner, so
the migrations themselves (grants included) are under test. Application sessions
connect as `chat_app`, the production role, so a missing grant fails a test.

Isolation, by default: each test runs inside one outer transaction that is rolled
back at teardown; a UnitOfWork "commit" only releases a savepoint. Tests marked
`@pytest.mark.real_commits` really commit, and the tables are truncated afterwards.

Note: chat_app has idle_in_transaction_session_timeout = 10s. Pausing in a debugger
inside a test for longer than that kills the test's connection.
"""

import os
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from alembic import command
from app.platform.db import SessionFactory
from app.platform.models_base import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]
APP_ROLE = "chat_app"
MIGRATION_LOCK_KEY = 815_001  # arbitrary, unique to this harness


@dataclass(frozen=True)
class DatabaseUnderTest:
    name: str
    owner_url: str  # postgresql+psycopg:// as the admin/owner, for Alembic and teardown
    app_url: str  # postgresql+asyncpg:// as chat_app, for the code under test


def _libpq_dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def admin_url() -> Iterator[str]:
    """A superuser URL (postgresql+psycopg://…/postgres) for the test server."""
    configured = os.environ.get("TEST_DATABASE_ADMIN_URL")
    if configured:
        yield configured
        return

    from testcontainers.community.postgres import PostgresContainer

    password = secrets.token_urlsafe(16)
    with PostgresContainer(
        "postgres:16-alpine", username="chat_owner", password=password, dbname="postgres"
    ) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        yield f"postgresql+psycopg://chat_owner:{password}@{host}:{port}/postgres"


@pytest.fixture(scope="session")
def app_role_password(admin_url: str) -> str:
    with psycopg.connect(_libpq_dsn(admin_url), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)).fetchone()
        if exists is None:
            password = secrets.token_urlsafe(16)
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(APP_ROLE), sql.Literal(password)
                )
            )
            return password
    # Never change the password of an existing role: on the compose server that would
    # break the running API.
    configured = os.environ.get("TEST_CHAT_APP_PASSWORD")
    if not configured:
        pytest.exit(
            "The chat_app role already exists on the test server; set TEST_CHAT_APP_PASSWORD "
            "to its password (docs/design/11 §21.3).",
            returncode=2,
        )
    return configured


def alembic_config(owner_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # ConfigParser interpolation: a literal % must be written as %%.
    config.set_main_option("sqlalchemy.url", owner_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
def test_database(
    admin_url: str, app_role_password: str, worker_id: str
) -> Iterator[DatabaseUnderTest]:
    name = f"test_{worker_id}"
    admin = make_url(admin_url)
    with psycopg.connect(_libpq_dsn(admin_url), autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    database = DatabaseUnderTest(
        name=name,
        owner_url=admin.set(database=name).render_as_string(hide_password=False),
        app_url=admin.set(
            drivername="postgresql+asyncpg",
            username=APP_ROLE,
            password=app_role_password,
            database=name,
        ).render_as_string(hide_password=False),
    )
    # xdist workers sharing one server would otherwise race on cluster-wide catalog
    # rows (e.g. ALTER ROLE). Advisory locks are per database, so take it in `postgres`,
    # which every worker's admin connection shares.
    with psycopg.connect(_libpq_dsn(admin_url), autocommit=True) as lock_conn:
        lock_conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            command.upgrade(alembic_config(database.owner_url), "head")
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    yield database

    with psycopg.connect(_libpq_dsn(admin_url), autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture
async def app_engine(test_database: DatabaseUnderTest) -> AsyncIterator[AsyncEngine]:
    # Function-scoped: asyncpg connections belong to the event loop that opened them.
    engine = create_async_engine(test_database.app_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


def truncate_all_tables(database: DatabaseUnderTest) -> None:
    tables = [table.name for table in Base.metadata.sorted_tables]
    if not tables:
        return
    statement = sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
        sql.SQL(", ").join(sql.Identifier(table) for table in tables)
    )
    with psycopg.connect(_libpq_dsn(database.owner_url), autocommit=True) as conn:
        conn.execute(statement)


@pytest.fixture
async def session_factory(
    request: pytest.FixtureRequest, app_engine: AsyncEngine, test_database: DatabaseUnderTest
) -> AsyncIterator[SessionFactory]:
    if request.node.get_closest_marker("real_commits"):
        try:
            yield async_sessionmaker(app_engine, expire_on_commit=False)
        finally:
            truncate_all_tables(test_database)
        return

    connection: AsyncConnection = await app_engine.connect()
    outer = await connection.begin()
    try:
        # Sessions join the outer transaction; their commits become savepoint releases.
        yield async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
    finally:
        await outer.rollback()
        await connection.close()


@pytest.fixture
async def session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """A session inside the per-test transaction, for repository-level tests."""
    async with session_factory() as db_session:
        yield db_session
