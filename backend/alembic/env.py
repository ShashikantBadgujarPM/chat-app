"""Alembic environment: async migrations run as the owner role.

URL resolution: `sqlalchemy.url` from the Alembic config (set by the test harness),
otherwise the DATABASE_OWNER_URL environment variable. `get_settings()` is not used on
purpose, because the migrate container has no JWT secret or app DATABASE_URL.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Importing the model modules registers their tables on Base.metadata for autogenerate.
import app.modules.identity.infrastructure.models
import app.platform.audit  # noqa: F401
from alembic import context
from app.platform.models_base import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    url = os.environ.get("DATABASE_OWNER_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_OWNER_URL must be set to run migrations (docs/design/12 §22.4)"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # Async psycopg can't use Windows' default ProactorEventLoop; the API itself
    # (asyncpg) is unaffected. Linux containers use the default loop.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run_migrations_online(), loop_factory=loop_factory)
