from typing import Any, cast

import pytest

from app.platform.db import SessionFactory, UnitOfWork


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_commit = False

    async def commit(self) -> None:
        self.calls.append("commit")
        if self.fail_commit:
            raise RuntimeError("commit failed")

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def close(self) -> None:
        self.calls.append("close")


def make_uow() -> tuple[UnitOfWork, FakeSession]:
    fake = FakeSession()
    factory = cast(SessionFactory, lambda: cast(Any, fake))
    return UnitOfWork(factory), fake


async def test_clean_exit_commits_then_closes() -> None:
    uow, session = make_uow()

    async with uow:
        pass

    assert session.calls == ["commit", "close"]


async def test_exception_rolls_back_closes_and_propagates() -> None:
    uow, session = make_uow()

    with pytest.raises(ValueError, match="boom"):
        async with uow:
            raise ValueError("boom")

    assert session.calls == ["rollback", "close"]


async def test_failed_commit_still_closes_and_propagates() -> None:
    uow, session = make_uow()
    session.fail_commit = True

    with pytest.raises(RuntimeError, match="commit failed"):
        async with uow:
            pass

    assert session.calls == ["commit", "close"]


async def test_session_outside_the_block_raises() -> None:
    uow, _ = make_uow()

    with pytest.raises(RuntimeError, match="outside"):
        _ = uow.session

    async with uow:
        assert uow.session is not None

    with pytest.raises(RuntimeError, match="outside"):
        _ = uow.session


async def test_entering_twice_raises() -> None:
    uow, _ = make_uow()

    async with uow:
        with pytest.raises(RuntimeError, match="already active"):
            await uow.__aenter__()


async def test_a_uow_can_be_reused_sequentially() -> None:
    uow, session = make_uow()

    async with uow:
        pass
    async with uow:
        pass

    assert session.calls == ["commit", "close", "commit", "close"]
