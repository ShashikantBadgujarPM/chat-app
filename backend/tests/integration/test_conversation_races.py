"""Concurrency guarantees for conversations (docs/design/13 R-2, R-12, R-14).

These use real commits and separate connections: the guards live in PostgreSQL locks
and constraints, which only interact across concurrent transactions.
"""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.modules.conversations.application.conversation_service import ConversationService
from app.modules.conversations.domain.errors import CannotRemoveLastOwner, NotOwner
from app.modules.conversations.infrastructure.models import (
    ConversationMemberModel,
    ConversationModel,
)
from app.modules.conversations.infrastructure.unit_of_work import SqlConversationsUnitOfWork
from app.platform.clock import SystemClock
from app.platform.db import SessionFactory, UnitOfWork
from tests.factories import make_user

pytestmark = pytest.mark.real_commits


def service(session_factory: SessionFactory) -> ConversationService:
    return ConversationService(lambda: SqlConversationsUnitOfWork(session_factory), SystemClock())


async def create_users(session_factory: SessionFactory, count: int) -> list[UUID]:
    async with UnitOfWork(session_factory) as uow:
        return [(await make_user(uow.session)).id for _ in range(count)]


async def test_concurrent_dm_creation_yields_one_conversation_r2(
    session_factory: SessionFactory,
) -> None:
    alice, bob = await create_users(session_factory, 2)
    conversations = service(session_factory)

    results = await asyncio.gather(
        *(
            conversations.get_or_create_direct(
                caller_id=alice if n % 2 else bob, other_id=bob if n % 2 else alice
            )
            for n in range(20)
        )
    )

    ids = {view.conversation.id for view, _ in results}
    assert len(ids) == 1
    assert sum(created for _, created in results) == 1
    async with session_factory() as session:
        total = (
            await session.execute(select(func.count()).select_from(ConversationModel))
        ).scalar()
        members = (
            await session.execute(select(func.count()).select_from(ConversationMemberModel))
        ).scalar()
    assert total == 1
    assert members == 2


async def test_two_owners_removing_each_other_leave_one_owner_r12(
    session_factory: SessionFactory,
) -> None:
    alice, bob = await create_users(session_factory, 2)
    conversations = service(session_factory)
    group = await conversations.create_group(caller_id=alice, title="Owners", member_ids=[bob])
    cid = group.conversation.id
    async with UnitOfWork(session_factory) as uow:
        await uow.session.execute(
            text("UPDATE conversation_members SET role = 'owner' WHERE conversation_id = :c"),
            {"c": cid},
        )

    outcomes = await asyncio.gather(
        conversations.remove_member(caller_id=alice, conversation_id=cid, user_id=bob),
        conversations.remove_member(caller_id=bob, conversation_id=cid, user_id=alice),
        return_exceptions=True,
    )

    succeeded = [o for o in outcomes if o is None]
    refused = [o for o in outcomes if isinstance(o, Exception)]
    assert len(succeeded) == 1
    assert len(refused) == 1
    assert isinstance(refused[0], CannotRemoveLastOwner | NotOwner)
    async with session_factory() as session:
        owners = (
            await session.execute(
                text(
                    "SELECT count(*) FROM conversation_members "
                    "WHERE conversation_id = :c AND role = 'owner' AND left_at IS NULL"
                ),
                {"c": cid},
            )
        ).scalar()
    assert owners == 1


async def test_last_owner_cannot_leave(session_factory: SessionFactory) -> None:
    alice, bob = await create_users(session_factory, 2)
    conversations = service(session_factory)
    group = await conversations.create_group(caller_id=alice, title="G", member_ids=[bob])

    with pytest.raises(CannotRemoveLastOwner):
        await conversations.remove_member(
            caller_id=alice, conversation_id=group.conversation.id, user_id=alice
        )


async def test_concurrent_adds_of_the_same_user_r14(session_factory: SessionFactory) -> None:
    owner, newcomer = await create_users(session_factory, 2)
    conversations = service(session_factory)
    group = await conversations.create_group(caller_id=owner, title="G", member_ids=[])
    cid = group.conversation.id

    results = await asyncio.gather(
        *(
            conversations.add_members(caller_id=owner, conversation_id=cid, user_ids=[newcomer])
            for _ in range(5)
        )
    )

    assert sum(len(r.added) for r in results) == 1
    assert sum(len(r.already_members) for r in results) == 4
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(func.count()).where(
                    ConversationMemberModel.conversation_id == cid,
                    ConversationMemberModel.user_id == newcomer,
                    ConversationMemberModel.left_at.is_(None),
                )
            )
        ).scalar()
    assert rows == 1


async def test_readding_a_former_member_resets_their_read_cursor_r14(
    session_factory: SessionFactory,
) -> None:
    owner, member = await create_users(session_factory, 2)
    conversations = service(session_factory)
    group = await conversations.create_group(caller_id=owner, title="G", member_ids=[member])
    cid = group.conversation.id
    await conversations.remove_member(caller_id=member, conversation_id=cid, user_id=member)
    async with UnitOfWork(session_factory) as uow:
        # Messages were sent while they were away (M05 maintains this counter).
        await uow.session.execute(
            text("UPDATE conversations SET last_message_seq = 42 WHERE id = :c"), {"c": cid}
        )

    result = await conversations.add_members(
        caller_id=owner, conversation_id=cid, user_ids=[member]
    )

    assert [m.user_id for m in result.added] == [member]
    async with session_factory() as session:
        left_at, last_read = (
            await session.execute(
                select(
                    ConversationMemberModel.left_at, ConversationMemberModel.last_read_seq
                ).where(
                    ConversationMemberModel.conversation_id == cid,
                    ConversationMemberModel.user_id == member,
                )
            )
        ).one()
    assert left_at is None
    assert last_read == 42
