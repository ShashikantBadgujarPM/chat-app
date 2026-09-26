import pytest
from sqlalchemy import func, select

from app.modules.identity.infrastructure.models import UserModel
from app.platform.db import SessionFactory, UnitOfWork
from tests.factories import make_user


async def count_users(session_factory: SessionFactory) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(UserModel))).scalar_one()


@pytest.mark.real_commits
async def test_clean_exit_commits(session_factory: SessionFactory) -> None:
    async with UnitOfWork(session_factory) as uow:
        await make_user(uow.session, username="committed")

    assert await count_users(session_factory) == 1


@pytest.mark.real_commits
async def test_exception_rolls_back_and_reraises(session_factory: SessionFactory) -> None:
    with pytest.raises(RuntimeError, match="abort"):  # noqa: PT012 - the block is the subject
        async with UnitOfWork(session_factory) as uow:
            await make_user(uow.session, username="rolled_back")
            raise RuntimeError("abort")

    assert await count_users(session_factory) == 0


@pytest.mark.real_commits
async def test_savepoint_rollback_keeps_the_outer_transaction_usable(
    session_factory: SessionFactory,
) -> None:
    async with UnitOfWork(session_factory) as uow:
        await make_user(uow.session, username="kept")
        with pytest.raises(RuntimeError, match="undo"):  # noqa: PT012 - the block is the subject
            async with uow.savepoint():
                await make_user(uow.session, username="undone")
                raise RuntimeError("undo")
        # The outer transaction continues after the savepoint rolled back.
        await make_user(uow.session, username="also_kept")

    async with session_factory() as session:
        names = set((await session.execute(select(UserModel.username))).scalars())
    assert names == {"kept", "also_kept"}


async def test_default_isolation_rolls_back_after_each_test(
    session_factory: SessionFactory,
) -> None:
    # Two tests share this username; each sees an empty table because the outer
    # transaction of the previous one was rolled back.
    async with UnitOfWork(session_factory) as uow:
        await make_user(uow.session, username="isolated")
    assert await count_users(session_factory) == 1


async def test_default_isolation_rolls_back_after_each_test_again(
    session_factory: SessionFactory,
) -> None:
    async with UnitOfWork(session_factory) as uow:
        await make_user(uow.session, username="isolated")
    assert await count_users(session_factory) == 1
