"""Database engine, sessions and the Unit of Work (docs/design/10 §18).

The Unit of Work is the only place in the application that commits or rolls back.
Services and repositories receive a session and never end a transaction themselves.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

from app.config import Settings

logger = logging.getLogger(__name__)

# Deterministic constraint and index names. Later milestones match expected integrity
# errors by these names (docs/design/10 §18), so they must not change.
NAMING_CONVENTION: dict[str, str] = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}

READINESS_DB_TIMEOUT_SECONDS = 2.0

SessionFactory = async_sessionmaker[AsyncSession]


def constraint_name(error: DBAPIError) -> str | None:
    """The name of the constraint or unique index a database error violated.

    Expected violations are matched by this name, never by message text
    (docs/design/10 §18). asyncpg exposes it on the driver exception that SQLAlchemy
    wraps (`constraint_name`); psycopg exposes it as `diag.constraint_name`.
    """
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None) or getattr(
            getattr(candidate, "diag", None), "constraint_name", None
        )
        if name:
            return str(name)
    return None


def create_engine(settings: Settings) -> AsyncEngine:
    engine = _create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Replaces connections broken by a DB restart, so readiness recovers by itself.
        pool_pre_ping=True,
    )
    logger.info(
        "Database engine created",
        extra={
            "event": "db.engine_created",
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
        },
    )
    return engine


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    # expire_on_commit=False: objects stay readable after commit without an implicit
    # (and, under asyncio, impossible) lazy reload.
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_database(engine: AsyncEngine) -> None:
    """Readiness check: raises if a pooled connection can't run SELECT 1 in time."""
    async with asyncio.timeout(READINESS_DB_TIMEOUT_SECONDS), engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


class UnitOfWork:
    """One database transaction.

        async with UnitOfWork(session_factory) as uow:
            repo = UserRepository(uow.session)
            ...

    A clean exit commits. An exception rolls back and propagates. The session is
    always closed.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork.session used outside `async with uow:`")
        return self._session

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("UnitOfWork is already active; create a new one instead")
        self._session = self._session_factory()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self.session
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """A nested transaction: an exception inside undoes only the block, then propagates."""
        async with self.session.begin_nested():
            yield
