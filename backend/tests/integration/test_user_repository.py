from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.user import UserStatus
from app.modules.identity.infrastructure.models import UserModel
from app.modules.identity.infrastructure.user_repository import UserRepository
from tests.factories import make_user


async def soft_delete(session: AsyncSession, username: str) -> None:
    await session.execute(
        update(UserModel).where(UserModel.username == username).values(deleted_at=datetime.now(UTC))
    )


async def test_add_returns_a_domain_user_with_database_defaults(session: AsyncSession) -> None:
    user = await UserRepository(session).add(
        username="alice",
        email="alice@example.test",
        display_name="Alice",
        password_hash="secret-hash-value",
    )

    assert user.id is not None
    assert user.status is UserStatus.ACTIVE
    assert user.timezone == "UTC"
    assert user.failed_login_attempts == 0
    assert user.created_at.tzinfo is not None
    assert user.is_active
    assert "password_hash" not in repr(user)
    assert "secret-hash-value" not in repr(user)


async def test_get_by_id(session: AsyncSession) -> None:
    created = await make_user(session)
    repository = UserRepository(session)

    assert await repository.get_by_id(created.id) == created
    assert await repository.get_by_id(uuid4()) is None


@pytest.mark.parametrize("identifier", ["Alice", "ALICE", "alice@EXAMPLE.test"])
async def test_get_by_username_or_email_is_case_insensitive(
    session: AsyncSession, identifier: str
) -> None:
    created = await make_user(session, username="alice", email="alice@example.test")

    found = await UserRepository(session).get_by_username_or_email(identifier)

    assert found is not None
    assert found.id == created.id


async def test_get_by_username_or_email_excludes_soft_deleted_users(session: AsyncSession) -> None:
    await make_user(session, username="gone")
    await soft_delete(session, "gone")

    assert await UserRepository(session).get_by_username_or_email("gone") is None


async def test_username_is_unique_case_insensitively(session: AsyncSession) -> None:
    await make_user(session, username="Alice")

    with pytest.raises(IntegrityError, match="uq_users_username_active"):
        await make_user(session, username="alice")


async def test_email_is_unique_case_insensitively(session: AsyncSession) -> None:
    await make_user(session, email="bob@example.test")

    with pytest.raises(IntegrityError, match="uq_users_email_active"):
        await make_user(session, email="BOB@example.test")


async def test_soft_deleting_a_user_frees_the_username(session: AsyncSession) -> None:
    await make_user(session, username="reused", email="first@example.test")
    await soft_delete(session, "reused")

    again = await make_user(session, username="reused", email="second@example.test")

    assert again.username == "reused"


async def test_updated_at_changes_on_orm_update(session: AsyncSession) -> None:
    user = await make_user(session)
    # now() is the transaction start time, so compare against a fixed earlier value.
    await session.execute(
        text("UPDATE users SET updated_at = '2000-01-01T00:00:00Z' WHERE id = :id"),
        {"id": user.id},
    )
    await session.execute(
        update(UserModel).where(UserModel.id == user.id).values(display_name="Renamed")
    )
    refreshed = await UserRepository(session).get_by_id(user.id)

    assert refreshed is not None
    assert refreshed.updated_at.year > 2000
