"""Persistence for users. Returns domain `User` objects, never ORM instances.

Repositories never commit; the caller's UnitOfWork owns the transaction.
"""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.user import User, UserStatus
from app.modules.identity.infrastructure.models import UserModel


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


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        row = await self._session.get(UserModel, user_id)
        return _to_domain(row) if row is not None else None

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
        row = UserModel(
            username=username,
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            timezone=timezone,
        )
        self._session.add(row)
        # Flush (not commit) so the database assigns the id and defaults now.
        await self._session.flush()
        await self._session.refresh(row)
        return _to_domain(row)
