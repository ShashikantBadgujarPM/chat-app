"""The scheduler against real PostgreSQL (docs/design/09 §17, 13 R-3/R-4/R-17; M11).

These tests really commit: SKIP LOCKED and row-lock waits only mean something across
separate transactions.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.config import Settings
from app.housekeeping import HousekeepingJob, outbox_retention, run_job
from app.modules.conversations.application.conversation_service import ConversationService
from app.modules.conversations.infrastructure.unit_of_work import SqlConversationsUnitOfWork
from app.modules.scheduling.application.scheduling_service import SchedulingService
from app.modules.scheduling.domain.errors import NotPending
from app.modules.scheduling.domain.scheduled_message import ScheduledMessage
from app.modules.scheduling.infrastructure.repository import ScheduledMessageRepository
from app.modules.scheduling.infrastructure.unit_of_work import SqlSchedulingUnitOfWork
from app.platform.clock import SystemClock
from app.platform.db import SessionFactory, UnitOfWork
from app.worker import Worker
from tests.conftest import SettingsFactory
from tests.factories import make_user
from tests.harness import DatabaseUnderTest

pytestmark = pytest.mark.real_commits


@pytest.fixture
def settings(make_settings: SettingsFactory, test_database: DatabaseUnderTest) -> Settings:
    return make_settings(database_url=test_database.app_url, scheduler_batch_size=20)


def worker(settings: Settings, session_factory: SessionFactory, **kwargs: object) -> Worker:
    return Worker(settings, session_factory, housekeeping=[], **kwargs)  # type: ignore[arg-type]


async def setup_chat(session_factory: SessionFactory) -> tuple[UUID, UUID, UUID]:
    """(alice, bob, conversation) where both are members."""
    async with UnitOfWork(session_factory) as uow:
        alice = (await make_user(uow.session)).id
        bob = (await make_user(uow.session)).id
    service = ConversationService(
        lambda: SqlConversationsUnitOfWork(session_factory), SystemClock()
    )
    view = await service.create_group(caller_id=alice, title="G", member_ids=[bob])
    return alice, bob, view.conversation.id


async def add_due(
    session_factory: SessionFactory,
    sender: UUID,
    conversation: UUID,
    *,
    count: int = 1,
    overdue: timedelta = timedelta(seconds=1),
) -> list[UUID]:
    """Insert scheduled rows that are already due (the API would refuse past times)."""
    ids = []
    async with UnitOfWork(session_factory) as uow:
        repo = ScheduledMessageRepository(uow.session)
        now = (await uow.session.execute(text("SELECT now()"))).scalar_one()
        for n in range(count):
            row = await repo.insert_on_conflict_client_id(
                conversation_id=conversation,
                sender_id=sender,
                body=f"scheduled {n}",
                reply_to_id=None,
                client_message_id=uuid4(),
                scheduled_at_utc=now - overdue,
                sender_timezone="UTC",
            )
            assert row is not None
            ids.append(row.id)
    return ids


async def fetch(session_factory: SessionFactory, scheduled_id: UUID) -> ScheduledMessage:
    async with UnitOfWork(session_factory) as uow:
        row = await ScheduledMessageRepository(uow.session).get(scheduled_id)
    assert row is not None
    return row


async def scalar(session_factory: SessionFactory, sql: str, **params: object) -> object:
    async with UnitOfWork(session_factory) as uow:
        return (await uow.session.execute(text(sql), params)).scalar_one()


def scheduling(session_factory: SessionFactory) -> SchedulingService:
    return SchedulingService(lambda: SqlSchedulingUnitOfWork(session_factory), SystemClock())


async def test_delivery_creates_the_message_and_events_in_one_commit(
    settings: Settings, session_factory: SessionFactory
) -> None:
    alice, _bob, cid = await setup_chat(session_factory)
    [sid] = await add_due(session_factory, alice, cid)

    assert await worker(settings, session_factory).run_batch() == 1

    row = await fetch(session_factory, sid)
    assert (row.status, row.attempts, row.last_error) == ("sent", 1, None)
    assert (
        await scalar(
            session_factory, "SELECT count(*) FROM messages WHERE scheduled_message_id = :s", s=sid
        )
        == 1
    )
    types = await scalar(
        session_factory,
        "SELECT array_agg(type ORDER BY id) FROM event_outbox "
        "WHERE type IN ('message.created', 'scheduled_message.sent')",
    )
    assert types == ["message.created", "scheduled_message.sent"]
    # A second run finds nothing to do.
    assert await worker(settings, session_factory).run_batch() == 0


async def test_n_workers_deliver_m_rows_exactly_once_r3(
    settings: Settings, session_factory: SessionFactory
) -> None:
    alice, _, cid = await setup_chat(session_factory)
    await add_due(session_factory, alice, cid, count=200)

    async def drain(w: Worker) -> None:
        while await w.run_batch():
            pass

    await asyncio.gather(*(drain(worker(settings, session_factory)) for _ in range(4)))

    assert await scalar(session_factory, "SELECT count(*) FROM messages") == 200
    assert (
        await scalar(
            session_factory,
            "SELECT count(DISTINCT scheduled_message_id) FROM messages",
        )
        == 200
    )
    assert (
        await scalar(
            session_factory, "SELECT count(*) FROM scheduled_messages WHERE status <> 'sent'"
        )
        == 0
    )
    # seq stayed gap-free under concurrent delivery (R-1).
    assert await scalar(session_factory, "SELECT max(seq) FROM messages") == 200


async def test_cancel_waits_for_a_worker_holding_the_row_then_409_r4(
    settings: Settings, session_factory: SessionFactory
) -> None:
    alice, _, cid = await setup_chat(session_factory)
    [sid] = await add_due(session_factory, alice, cid)
    cancel_outcome: list[object] = []

    async def cancel() -> None:
        try:
            await scheduling(session_factory).cancel(caller_id=alice, scheduled_id=sid)
            cancel_outcome.append("cancelled")
        except NotPending:
            cancel_outcome.append("not_pending")

    cancel_task: asyncio.Task[None] | None = None

    async def on_claimed(rows: Sequence[ScheduledMessage]) -> None:
        nonlocal cancel_task
        cancel_task = asyncio.create_task(cancel())
        await asyncio.sleep(0.3)
        assert not cancel_task.done(), "cancel should block on the worker's row lock"

    await worker(settings, session_factory, on_claimed=on_claimed).run_batch()
    assert cancel_task is not None
    await cancel_task

    assert cancel_outcome == ["not_pending"]
    assert (await fetch(session_factory, sid)).status == "sent"


async def test_cancel_first_means_it_is_never_delivered_r4(
    settings: Settings, session_factory: SessionFactory
) -> None:
    alice, _, cid = await setup_chat(session_factory)
    [sid] = await add_due(session_factory, alice, cid)

    await scheduling(session_factory).cancel(caller_id=alice, scheduled_id=sid)

    assert await worker(settings, session_factory).run_batch() == 0
    assert await scalar(session_factory, "SELECT count(*) FROM messages") == 0


async def test_failure_after_the_insert_rolls_back_the_message_and_events(
    settings: Settings, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice, _, cid = await setup_chat(session_factory)
    [sid] = await add_due(session_factory, alice, cid)

    async def boom(self: ScheduledMessageRepository, scheduled_id: UUID) -> ScheduledMessage:
        raise RuntimeError("injected after the message insert")

    monkeypatch.setattr(ScheduledMessageRepository, "mark_sent", boom)
    await worker(settings, session_factory).run_batch()

    assert await scalar(session_factory, "SELECT count(*) FROM messages") == 0
    assert (
        await scalar(
            session_factory, "SELECT count(*) FROM event_outbox WHERE type = 'message.created'"
        )
        == 0
    )
    row = await fetch(session_factory, sid)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "RuntimeError")
    assert row.next_attempt_at > datetime.now(UTC)


async def test_one_failing_item_does_not_undo_the_rest_of_the_batch(
    settings: Settings, session_factory: SessionFactory
) -> None:
    alice, bob, cid = await setup_chat(session_factory)
    first = await add_due(session_factory, alice, cid, overdue=timedelta(seconds=30))
    middle = await add_due(session_factory, bob, cid, overdue=timedelta(seconds=20))
    last = await add_due(session_factory, alice, cid, overdue=timedelta(seconds=10))
    await ConversationService(
        lambda: SqlConversationsUnitOfWork(session_factory), SystemClock()
    ).remove_member(caller_id=bob, conversation_id=cid, user_id=bob)

    assert await worker(settings, session_factory).run_batch() == 3

    statuses = [(await fetch(session_factory, s[0])).status for s in (first, middle, last)]
    assert statuses == ["sent", "failed", "sent"]
    failed = await fetch(session_factory, middle[0])
    assert failed.last_error == "sender_not_member"
    assert (
        await scalar(
            session_factory,
            "SELECT count(*) FROM event_outbox WHERE type = 'scheduled_message.failed'",
        )
        == 1
    )


async def test_more_than_24h_overdue_fails_as_expired(
    settings: Settings, session_factory: SessionFactory
) -> None:
    alice, _, cid = await setup_chat(session_factory)
    [sid] = await add_due(session_factory, alice, cid, overdue=timedelta(hours=25))

    await worker(settings, session_factory).run_batch()

    row = await fetch(session_factory, sid)
    assert (row.status, row.last_error) == ("failed", "expired")
    assert await scalar(session_factory, "SELECT count(*) FROM messages") == 0


async def test_transient_failures_stop_at_max_attempts(
    settings: Settings, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice, _, cid = await setup_chat(session_factory)
    [sid] = await add_due(session_factory, alice, cid)
    await scalar(
        session_factory,
        "UPDATE scheduled_messages SET attempts = 4 WHERE id = :s RETURNING attempts",
        s=sid,
    )

    async def boom(self: ScheduledMessageRepository, scheduled_id: UUID) -> ScheduledMessage:
        raise RuntimeError("still broken")

    monkeypatch.setattr(ScheduledMessageRepository, "mark_sent", boom)
    await worker(settings, session_factory).run_batch()

    row = await fetch(session_factory, sid)
    assert (row.status, row.attempts, row.last_error) == ("failed", 5, "max_attempts_exceeded")


async def test_sigterm_mid_batch_commits_the_batch_and_stops(
    settings: Settings, session_factory: SessionFactory
) -> None:
    alice, _, cid = await setup_chat(session_factory)
    await add_due(session_factory, alice, cid, count=3)
    w: Worker

    async def on_claimed(rows: Sequence[ScheduledMessage]) -> None:
        w.request_stop()  # arrives while the batch holds its row locks

    w = worker(settings, session_factory, on_claimed=on_claimed)
    await asyncio.wait_for(w.run(), timeout=10)

    assert await scalar(session_factory, "SELECT count(*) FROM messages") == 3


async def test_outbox_retention_deletes_only_old_rows(session_factory: SessionFactory) -> None:
    async with UnitOfWork(session_factory) as uow:
        await uow.session.execute(
            text(
                "INSERT INTO event_outbox (type, recipient_user_ids, payload, created_at) VALUES "
                "('old', ARRAY[gen_random_uuid()], '{}', now() - interval '8 days'), "
                "('new', ARRAY[gen_random_uuid()], '{}', now() - interval '6 days')"
            )
        )
    job = HousekeepingJob("t", advisory_key=900_001, interval_seconds=1, batch=outbox_retention(7))

    assert await run_job(session_factory, job) == 1
    assert await scalar(session_factory, "SELECT array_agg(type) FROM event_outbox") == ["new"]


async def test_a_housekeeping_job_runs_on_only_one_worker_at_a_time_r17(
    session_factory: SessionFactory,
) -> None:
    ran: list[str] = []
    release = asyncio.Event()

    async def slow_batch(session: object) -> int:
        ran.append("ran")
        await release.wait()
        return 0

    job = HousekeepingJob("t", advisory_key=900_002, interval_seconds=1, batch=slow_batch)  # type: ignore[arg-type]
    first = asyncio.create_task(run_job(session_factory, job))
    await asyncio.sleep(0.3)  # first now holds the advisory lock

    assert await run_job(session_factory, job) is None  # skipped, lock held elsewhere
    release.set()
    assert await first == 0
    assert ran == ["ran"]
