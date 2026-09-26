"""Reconnect sync and session revocation (M09, R-9, R-16)."""

import asyncio
from typing import Any
from uuid import uuid4

import asyncpg
import psycopg
import pytest

from app.realtime.outbox_listener import libpq_dsn
from tests.harness import DatabaseUnderTest, _libpq_dsn
from tests.ws.conftest import LiveServer, Socket, WsUser

pytestmark = pytest.mark.slow


async def dm(server: LiveServer, a: WsUser, b: WsUser) -> str:
    response = await server.http.post(
        "/api/v1/conversations/direct", json={"user_id": b.id}, headers=a.headers
    )
    cid: str = response.json()["id"]
    return cid


async def send(server: LiveServer, user: WsUser, cid: str, body: str = "hi") -> dict[str, Any]:
    response = await server.http.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"client_message_id": str(uuid4()), "body": body},
        headers=user.headers,
    )
    assert response.status_code == 201, response.text
    message: dict[str, Any] = response.json()
    return message


async def sync(sock: Socket, after: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Send sync.request; collect replayed events until the terminal frame."""
    await sock.send({"type": "sync.request", "payload": {"after_event_id": after}})
    events: list[dict[str, Any]] = []
    while True:
        frame = await sock.recv(timeout=10)
        if frame["type"] == "sync.batch":
            events.extend(frame["payload"]["events"])
        elif frame["type"] in ("sync.complete", "sync.reset_required"):
            return events, frame


async def latest_id(sock: Socket, cid: str) -> int:
    event = await sock.recv_type("message.created")
    assert event["conversation_id"] == cid
    event_id: int = event["id"]
    return event_id


async def test_reconnect_replays_exactly_what_was_missed(live_server: LiveServer) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    cid = await dm(live_server, alice, bob)
    bob_tab = await bob.online()
    await send(live_server, alice, cid, "before")
    cursor = await latest_id(bob_tab, cid)
    await bob_tab.close()

    missed = [await send(live_server, alice, cid, f"missed {n}") for n in range(30)]
    bob_again = await bob.online()
    events, done = await sync(bob_again, cursor)

    replayed_messages = [
        e["payload"]["id"] for e in events if e["type"] == "message.created" and e["id"] > cursor
    ]
    assert replayed_messages == [m["id"] for m in missed]
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)  # id order
    assert done["type"] == "sync.complete"
    assert done["payload"]["latest_event_id"] >= max(ids)


async def test_null_cursor_completes_at_once(live_server: LiveServer) -> None:
    sock = await (await live_server.user()).online()

    events, done = await sync(sock, None)

    assert events == []
    assert done["type"] == "sync.complete"


async def test_pruned_cursor_requires_a_reset(
    live_server: LiveServer, test_database: DatabaseUnderTest
) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    cid = await dm(live_server, alice, bob)
    for _ in range(3):
        await send(live_server, alice, cid)
    with psycopg.connect(_libpq_dsn(test_database.owner_url), autocommit=True) as conn:
        # Retention pruning removed everything up to the latest event.
        conn.execute("DELETE FROM event_outbox WHERE id < (SELECT max(id) FROM event_outbox)")
    sock = await bob.online()

    events, done = await sync(sock, 1)

    assert done["type"] == "sync.reset_required"
    assert done["payload"]["reason"] == "cursor_expired"


async def test_live_events_during_a_slow_replay_arrive_once_after_complete_r16(
    live_server: LiveServer,
) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    cid = await dm(live_server, alice, bob)
    bob_tab = await bob.online()
    await send(live_server, alice, cid)
    cursor = await latest_id(bob_tab, cid)
    await bob_tab.close()
    await send(live_server, alice, cid, "missed")

    paused, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        paused.set()
        await release.wait()

    live_server.app.state.sync.before_complete = hold
    bob_again = await bob.online()
    await bob_again.send({"type": "sync.request", "payload": {"after_event_id": cursor}})
    await asyncio.wait_for(paused.wait(), 5)
    during = await send(live_server, alice, cid, "sent during replay")
    await asyncio.sleep(0.3)  # let the listener buffer it
    release.set()

    frames = []
    while True:
        frame = await bob_again.recv(timeout=5)
        frames.append(frame)
        if frame["type"] == "sync.complete":
            break
    after_complete = await bob_again.recv_type("message.created")
    await bob_again.none_of_type("message.created", 0.5)

    assert all(f["type"] != "message.created" for f in frames)  # nothing jumped the replay
    assert after_complete["payload"]["id"] == during["id"]  # delivered exactly once


async def test_an_out_of_order_commit_is_recovered_by_the_overlap_window_r9(
    live_server: LiveServer, test_database: DatabaseUnderTest
) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    bob_tab = await bob.online()
    dsn = libpq_dsn(test_database.app_url)
    insert = (
        "INSERT INTO event_outbox (type, recipient_user_ids, payload) "
        "VALUES ($1, ARRAY[$2::uuid], '{}') RETURNING id"
    )
    slow, fast = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
    try:
        slow_tx = slow.transaction()
        await slow_tx.start()
        first_id = await slow.fetchval(insert, "test.late", bob.id)  # id N, not committed
        second_id = await fast.fetchval(insert, "test.early", bob.id)  # id N+1, committed
        assert second_id > first_id

        early = await bob_tab.recv_type("test.early")
        assert early["id"] == second_id
        await bob_tab.close()  # the client's cursor is now N+1
        await slow_tx.commit()  # N commits *after* N+1
    finally:
        await slow.close()
        await fast.close()

    bob_again = await bob.online()
    events, done = await sync(bob_again, second_id)

    assert first_id in [e["id"] for e in events]  # not skipped
    assert done["type"] == "sync.complete"


async def test_listener_reconnect_tells_every_client_to_sync(
    live_server: LiveServer, test_database: DatabaseUnderTest
) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    alice_tab, bob_tab = await alice.online(), await bob.online()

    with psycopg.connect(_libpq_dsn(test_database.owner_url), autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE query LIKE 'LISTEN%%' AND datname = %s",
            (test_database.name,),
        )

    for sock in (alice_tab, bob_tab):
        frame = await sock.recv_type("sync.required", timeout=15)
        assert frame["payload"] == {"reason": "listener_reconnected"}


async def test_logout_closes_that_sessions_sockets_only(live_server: LiveServer) -> None:
    user = await live_server.user()
    other_browser = await user.new_session()
    tab_1, tab_2 = await user.online(), await user.online()
    elsewhere = await other_browser.online()
    ticket_response = await live_server.http.post("/api/v1/auth/ws-ticket", headers=user.headers)
    assert ticket_response.status_code == 201

    # Log out session S (the refresh cookie identifies it): use its refresh token.
    await live_server.http.post("/api/v1/auth/logout-all", headers=other_browser.headers)

    for sock in (tab_1, tab_2, elsewhere):
        assert await sock.close_code() == 4001


async def test_revoking_one_session_leaves_the_other_open(live_server: LiveServer) -> None:
    user = await live_server.user()
    other_browser = await user.new_session()
    tab_1, tab_2 = await user.online(), await user.online()
    elsewhere = await other_browser.online()
    sessions = (
        await live_server.http.get("/api/v1/auth/sessions", headers=other_browser.headers)
    ).json()
    [first_session] = [s for s in sessions["items"] if not s["current"]]

    await live_server.http.delete(
        f"/api/v1/auth/sessions/{first_session['family_id']}", headers=other_browser.headers
    )

    for sock in (tab_1, tab_2):
        closed = sock.ws
        assert await sock.close_code() == 4001
        assert closed.close_reason == "session_revoked"
    await elsewhere.send({"type": "ping"})
    assert (await elsewhere.recv_type("pong"))["type"] == "pong"  # still connected


async def test_rest_fallback_matches_the_ws_replay(live_server: LiveServer) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    cid = await dm(live_server, alice, bob)
    bob_tab = await bob.online()
    await send(live_server, alice, cid)
    cursor = await latest_id(bob_tab, cid)
    for n in range(5):
        await send(live_server, alice, cid, f"m{n}")

    ws_events, _ = await sync(bob_tab, cursor)
    rest = await live_server.http.get(
        f"/api/v1/sync/events?after_event_id={cursor}", headers=bob.headers
    )

    body = rest.json()
    assert body["reset_required"] is False
    assert [e["id"] for e in body["events"]] == [e["id"] for e in ws_events]


async def test_outbox_transactions_are_time_bounded(live_server: LiveServer) -> None:
    """The overlap window depends on these bounds (08 §14.5)."""
    from sqlalchemy import text

    from app.realtime.publisher import OutboxEventPublisher

    factory = live_server.app.state.session_factory
    async with factory() as session, session.begin():
        await OutboxEventPublisher(session).publish(
            "test.bounds", recipient_user_ids=[uuid4()], payload={}
        )
        statement_timeout = (await session.execute(text("SHOW statement_timeout"))).scalar()
        idle_timeout = (
            await session.execute(text("SHOW idle_in_transaction_session_timeout"))
        ).scalar()
        await session.rollback()

    assert statement_timeout == "5s"
    assert idle_timeout == "10s"
