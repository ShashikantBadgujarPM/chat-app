"""A connection never receives an event from before it registered (docs/design/08
§14.4). `test_large_groups_get_no_receipts` in test_read_events.py exercises this
through the real NOTIFY race, which only reliably reproduces the bug under load; this
file re-delivers a known pre-connection event directly through the listener's real
dispatch path, so the connection watermark is verified deterministically."""

from uuid import uuid4

from sqlalchemy import func, select

from app.realtime.publisher import EventOutboxModel
from tests.ws.conftest import LiveServer


async def latest_event_id(server: LiveServer) -> int:
    async with server.app.state.engine.connect() as conn:
        return int((await conn.execute(select(func.max(EventOutboxModel.id)))).scalar_one())


async def test_a_replayed_pre_connection_event_is_not_delivered(live_server: LiveServer) -> None:
    """Even if the listener (re)dispatches an event committed before a connection
    registered -- exactly what a delayed micro-batch dispatch racing a fast reconnect
    can cause -- the connection's watermark must still suppress it."""
    alice, bob = await live_server.user(), await live_server.user()
    cid = (
        await live_server.http.post(
            "/api/v1/conversations/direct", json={"user_id": bob.id}, headers=alice.headers
        )
    ).json()["id"]
    await live_server.http.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"client_message_id": str(uuid4()), "body": "before bob connects"},
        headers=alice.headers,
    )
    pre_connection_event_id = await latest_event_id(live_server)

    bob_tab = await bob.online()  # registers with a watermark at/after that event's id

    # Simulate the listener dispatching (or redelivering) that already-old event,
    # bypassing NOTIFY timing entirely so this doesn't depend on winning a race.
    await live_server.app.state.outbox_listener.dispatch([pre_connection_event_id])

    await bob_tab.nothing_within(0.5)


async def test_a_genuinely_new_event_is_still_delivered(live_server: LiveServer) -> None:
    """The watermark is a floor, not a blanket suppression: an event committed after
    registration reaches the connection as normal."""
    alice, bob = await live_server.user(), await live_server.user()
    cid = (
        await live_server.http.post(
            "/api/v1/conversations/direct", json={"user_id": bob.id}, headers=alice.headers
        )
    ).json()["id"]
    bob_tab = await bob.online()

    await live_server.http.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"client_message_id": str(uuid4()), "body": "after bob connects"},
        headers=alice.headers,
    )

    frame = await bob_tab.recv_type("message.created")
    assert frame["payload"]["body"] == "after bob connects"
