"""Messaging write path against real PostgreSQL (docs/design/13 R-1, R-5; M05)."""

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.modules.conversations.application.conversation_service import ConversationService
from app.modules.conversations.infrastructure.unit_of_work import SqlConversationsUnitOfWork
from app.modules.messaging.application.messaging_service import MessagingService
from app.modules.messaging.infrastructure.models import MessageModel
from app.modules.messaging.infrastructure.unit_of_work import SqlMessagingUnitOfWork
from app.platform.clock import SystemClock
from app.platform.db import SessionFactory, UnitOfWork
from tests.factories import make_user


@dataclass
class RecordedEvent:
    type: str
    conversation_id: UUID | None
    recipients: set[UUID]
    payload: Mapping[str, Any]


@dataclass
class RecordingPublisher:
    events: list[RecordedEvent] = field(default_factory=list)

    async def publish(
        self,
        event_type: str,
        *,
        recipient_user_ids: Iterable[UUID],
        payload: Mapping[str, Any],
        conversation_id: UUID | None = None,
    ) -> None:
        self.events.append(
            RecordedEvent(event_type, conversation_id, set(recipient_user_ids), payload)
        )


async def users(session_factory: SessionFactory, count: int) -> list[UUID]:
    async with UnitOfWork(session_factory) as uow:
        return [(await make_user(uow.session)).id for _ in range(count)]


async def group(session_factory: SessionFactory, owner: UUID, members: list[UUID]) -> UUID:
    service = ConversationService(
        lambda: SqlConversationsUnitOfWork(session_factory), SystemClock()
    )
    view = await service.create_group(caller_id=owner, title="G", member_ids=members)
    return view.conversation.id


def messaging(
    session_factory: SessionFactory, publisher: RecordingPublisher | None = None
) -> MessagingService:
    class RecordingUnitOfWork(SqlMessagingUnitOfWork):
        # `async with` looks __aenter__ up on the type, so override it in a subclass.
        async def __aenter__(self) -> "RecordingUnitOfWork":
            await super().__aenter__()
            if publisher is not None:
                self.events = publisher
            return self

    return MessagingService(lambda: RecordingUnitOfWork(session_factory), SystemClock())


@pytest.mark.real_commits
async def test_concurrent_sends_get_gap_free_seqs_r1(session_factory: SessionFactory) -> None:
    alice, bob = await users(session_factory, 2)
    cid = await group(session_factory, alice, [bob])
    service = messaging(session_factory)

    await asyncio.gather(
        *(
            service.send(
                sender_id=alice if n % 2 else bob,
                conversation_id=cid,
                body=f"message {n}",
                reply_to_id=None,
                client_message_id=uuid4(),
            )
            for n in range(50)
        )
    )

    async with session_factory() as session:
        seqs = sorted(
            (
                await session.execute(
                    select(MessageModel.seq).where(MessageModel.conversation_id == cid)
                )
            ).scalars()
        )
        last = (
            await session.execute(
                text("SELECT last_message_seq FROM conversations WHERE id = :c"), {"c": cid}
            )
        ).scalar()
    assert seqs == list(range(1, 51))
    assert last == 50


@pytest.mark.real_commits
async def test_concurrent_replays_create_one_message_and_no_seq_gap_r5(
    session_factory: SessionFactory,
) -> None:
    alice, bob = await users(session_factory, 2)
    cid = await group(session_factory, alice, [bob])
    service = messaging(session_factory)
    client_id = uuid4()

    results = await asyncio.gather(
        *(
            service.send(
                sender_id=alice,
                conversation_id=cid,
                body="only once",
                reply_to_id=None,
                client_message_id=client_id,
            )
            for _ in range(10)
        )
    )
    after = await service.send(
        sender_id=alice,
        conversation_id=cid,
        body="next",
        reply_to_id=None,
        client_message_id=uuid4(),
    )

    assert len({view.message.id for view, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    # A lost race rolls its seq increment back with the savepoint: no gap.
    assert after[0].message.seq == 2


@pytest.mark.real_commits
async def test_sends_to_different_conversations_do_not_serialize(
    session_factory: SessionFactory,
) -> None:
    """Timing sanity check (not a strict assertion): 5 conversations proceed in parallel."""
    alice, bob = await users(session_factory, 2)
    conversations = [await group(session_factory, alice, [bob]) for _ in range(5)]
    service = messaging(session_factory)

    await asyncio.gather(
        *(
            service.send(
                sender_id=alice,
                conversation_id=conversations[n % 5],
                body="x",
                reply_to_id=None,
                client_message_id=uuid4(),
            )
            for n in range(50)
        )
    )

    async with session_factory() as session:
        per_conversation = (
            await session.execute(
                text(
                    "SELECT conversation_id, max(seq), count(*) FROM messages "
                    "GROUP BY conversation_id"
                )
            )
        ).all()
    assert sorted((m, c) for _, m, c in per_conversation) == [(10, 10)] * 5


async def test_events_have_the_right_type_payload_and_recipients(
    session_factory: SessionFactory,
) -> None:
    alice, bob, carol = await users(session_factory, 3)
    cid = await group(session_factory, alice, [bob, carol])
    # Carol leaves: she must not receive later events.
    await ConversationService(
        lambda: SqlConversationsUnitOfWork(session_factory), SystemClock()
    ).remove_member(caller_id=carol, conversation_id=cid, user_id=carol)
    publisher = RecordingPublisher()
    service = messaging(session_factory, publisher)

    view, _ = await service.send(
        sender_id=alice,
        conversation_id=cid,
        body="  hello  ",
        reply_to_id=None,
        client_message_id=uuid4(),
    )
    await service.edit_message(caller_id=alice, message_id=view.message.id, body="hello again")
    await service.delete_message(caller_id=alice, message_id=view.message.id)

    created, updated, deleted = publisher.events
    assert [e.type for e in publisher.events] == [
        "message.created",
        "message.updated",
        "message.deleted",
    ]
    for event in publisher.events:
        assert event.conversation_id == cid
        assert event.recipients == {alice, bob}
    assert created.payload["body"] == "hello"  # trimmed
    assert created.payload["seq"] == 1
    assert created.payload["sender"]["id"] == str(alice)
    assert updated.payload["body"] == "hello again"
    assert updated.payload["edited_at"] is not None
    assert set(deleted.payload) == {"id", "conversation_id", "seq", "deleted_at"}


async def test_iter_history_walks_every_page(session_factory: SessionFactory) -> None:
    alice, bob = await users(session_factory, 2)
    cid = await group(session_factory, alice, [bob])
    service = messaging(session_factory)
    for n in range(7):
        await service.send(
            sender_id=alice,
            conversation_id=cid,
            body=f"m{n}",
            reply_to_id=None,
            client_message_id=uuid4(),
        )

    pages = [
        [view.message.seq for view in page]
        async for page in service.iter_history(caller_id=bob, conversation_id=cid, page_size=3)
    ]

    assert pages == [[7, 6, 5], [4, 3, 2], [1]]


async def test_history_uses_the_conversation_seq_index(session_factory: SessionFactory) -> None:
    alice, bob = await users(session_factory, 2)
    cid = await group(session_factory, alice, [bob])

    async with session_factory() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in await session.execute(
                text(
                    "EXPLAIN SELECT * FROM messages WHERE conversation_id = :c AND seq < 100 "
                    "ORDER BY seq DESC LIMIT 51"
                ),
                {"c": cid},
            )
        )
    assert "Index Scan" in plan
    assert "messages_conversation_id_seq" in plan
