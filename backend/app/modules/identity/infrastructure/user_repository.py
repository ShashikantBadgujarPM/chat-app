"""Persistence for users. Returns domain `User` objects, never ORM instances.

Repositories never commit; the caller's UnitOfWork owns the transaction.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.errors import EmailTaken, UsernameTaken
from app.modules.identity.domain.user import User, UserStatus
from app.modules.identity.infrastructure.models import UserModel
from app.platform.db import constraint_name

# Expected uniqueness violations, matched by name (docs/design/10 §18).
_CONFLICTS = {
    "uq_users_username_active": UsernameTaken,
    "uq_users_email_active": EmailTaken,
}

# R-18: one atomic statement, so concurrent failures can't lose increments and slip
# past the lockout. A lock that has already expired starts a fresh count. `:now` comes
# from the Clock port, so lockout expiry is testable with a fake clock (Q-010).
_RECORD_FAILED_LOGIN = text(
    """
    UPDATE users SET
        failed_login_attempts = CASE
            WHEN locked_until IS NOT NULL AND locked_until <= :now THEN 1
            ELSE failed_login_attempts + 1
        END,
        locked_until = CASE
            WHEN (CASE
                    WHEN locked_until IS NOT NULL AND locked_until <= :now THEN 1
                    ELSE failed_login_attempts + 1
                  END) >= :max_attempts THEN :lock_until
            WHEN locked_until IS NOT NULL AND locked_until <= :now THEN NULL
            ELSE locked_until
        END,
        updated_at = now()
    WHERE id = :user_id
    RETURNING failed_login_attempts, locked_until
    """
)


# Trigram search (docs/design/05 §users, Q-006). Both the filter and the score use
# exactly `username::text` and `display_name`, so the GIN indexes serve the query:
# ILIKE for substrings, `<%` (word similarity) for typos. Keyset pagination on
# (score DESC, id ASC) keeps pages stable when users register between fetches.
_SEARCH = text(
    """
    SELECT u.*, s.score
    FROM users AS u
    CROSS JOIN LATERAL (
        SELECT GREATEST(
            word_similarity(:q, u.username::text),
            word_similarity(:q, u.display_name)
        )::float8 AS score
    ) AS s
    WHERE u.deleted_at IS NULL
      AND u.status = 'active'
      AND u.id <> :exclude_id
      AND (
          u.username::text ILIKE :pattern ESCAPE '!'
          OR u.display_name ILIKE :pattern ESCAPE '!'
          OR :q <% u.username::text
          OR :q <% u.display_name
      )
      AND (
          CAST(:after_score AS float8) IS NULL
          OR s.score < :after_score
          OR (s.score = :after_score AND u.id > CAST(:after_id AS uuid))
      )
    ORDER BY s.score DESC, u.id ASC
    LIMIT :limit
    """
)


def _like_pattern(query: str) -> str:
    """Match `query` as a literal substring: its own % and _ are not wildcards."""
    escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def _to_domain(row: UserModel) -> User:
    return User(
        id=row.id,
        username=row.username,
        email=row.email,
        display_name=row.display_name,
        password_hash=row.password_hash,
        status=UserStatus(row.status),
        timezone=row.timezone,
        failed_login_attempts=row.failed_login_attempts,
        locked_until=row.locked_until,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _row_to_domain(row: Any) -> User:
    """A User from a raw `users` row mapping (text() queries)."""
    return User(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        display_name=row["display_name"],
        password_hash=row["password_hash"],
        status=UserStatus(row["status"]),
        timezone=row["timezone"],
        failed_login_attempts=row["failed_login_attempts"],
        locked_until=row["locked_until"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        row = await self._session.get(UserModel, user_id)
        return _to_domain(row) if row is not None else None

    async def get_active_by_id(self, user_id: UUID) -> User | None:
        """An active, non-deleted user, as other users may see them."""
        statement = select(UserModel).where(
            UserModel.id == user_id,
            UserModel.deleted_at.is_(None),
            UserModel.status == UserStatus.ACTIVE.value,
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    async def search(
        self,
        query: str,
        *,
        exclude_id: UUID,
        after: tuple[float, UUID] | None,
        limit: int,
    ) -> list[tuple[User, float]]:
        result = await self._session.execute(
            _SEARCH,
            {
                "q": query,
                "pattern": _like_pattern(query),
                "exclude_id": exclude_id,
                "after_score": after[0] if after else None,
                "after_id": after[1] if after else None,
                "limit": limit,
            },
        )
        return [(_row_to_domain(row._mapping), float(row.score)) for row in result]

    async def update_profile(
        self, user_id: UUID, *, display_name: str | None = None, timezone: str | None = None
    ) -> User:
        values: dict[str, str] = {}
        if display_name is not None:
            values["display_name"] = display_name
        if timezone is not None:
            values["timezone"] = timezone
        if values:
            await self._session.execute(
                update(UserModel).where(UserModel.id == user_id).values(**values)
            )
        row = await self._session.get(UserModel, user_id, populate_existing=True)
        assert row is not None  # noqa: S101 - the caller is that authenticated user
        return _to_domain(row)

    async def soft_delete(self, user_id: UUID, *, now: datetime) -> None:
        await self._session.execute(
            update(UserModel).where(UserModel.id == user_id).values(deleted_at=now)
        )

    async def get_by_username_or_email(self, identifier: str) -> User | None:
        """Case-insensitive (citext) match on username or email; soft-deleted users excluded."""
        statement = select(UserModel).where(
            or_(UserModel.username == identifier, UserModel.email == identifier),
            UserModel.deleted_at.is_(None),
        )
        row = (await self._session.execute(statement)).scalars().first()
        return _to_domain(row) if row is not None else None

    async def add(
        self,
        *,
        username: str,
        email: str,
        display_name: str,
        password_hash: str,
        timezone: str = "UTC",
    ) -> User:
        """Raises UsernameTaken / EmailTaken on a uniqueness conflict."""
        row = UserModel(
            username=username,
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            timezone=timezone,
        )
        self._session.add(row)
        try:
            # Flush (not commit) so the database assigns the id and defaults now.
            await self._session.flush()
        except IntegrityError as exc:
            conflict = _CONFLICTS.get(constraint_name(exc) or "")
            if conflict is None:
                raise
            raise conflict() from exc
        await self._session.refresh(row)
        return _to_domain(row)

    async def record_failed_login(
        self, user_id: UUID, *, now: datetime, max_attempts: int, lock_for: timedelta
    ) -> tuple[int, datetime | None]:
        result = await self._session.execute(
            _RECORD_FAILED_LOGIN,
            {
                "user_id": user_id,
                "now": now,
                "max_attempts": max_attempts,
                "lock_until": now + lock_for,
            },
        )
        attempts, locked_until = result.one()
        return int(attempts), locked_until

    async def reset_failed_logins(self, user_id: UUID) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(failed_login_attempts=0, locked_until=None)
        )

    async def set_password_hash(self, user_id: UUID, password_hash: str) -> None:
        await self._session.execute(
            update(UserModel).where(UserModel.id == user_id).values(password_hash=password_hash)
        )
