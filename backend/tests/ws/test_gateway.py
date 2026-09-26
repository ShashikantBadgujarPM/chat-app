"""The /ws gateway: handshake, protocol, fan-out, backpressure (M06)."""

import asyncio
import logging
from uuid import UUID, uuid4

import psycopg
import pytest
from websockets.exceptions import InvalidStatus

from tests.harness import DatabaseUnderTest, _libpq_dsn
from tests.ws.conftest import LiveServer, WsUser

pytestmark = pytest.mark.slow

ENVELOPE_KEYS = {"v", "id", "type", "conversation_id", "occurred_at", "correlation_id", "payload"}


async def group(server: LiveServer, owner: WsUser, *members: WsUser) -> str:
    response = await server.http.post(
        "/api/v1/conversations/groups",
        json={"title": "Team", "member_ids": [m.id for m in members]},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    conversation_id: str = response.json()["id"]
    return conversation_id


async def send(server: LiveServer, user: WsUser, cid: str, body: str) -> dict[str, object]:
    response = await server.http.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"client_message_id": str(uuid4()), "body": body},
        headers=user.headers,
    )
    assert response.status_code == 201, response.text
    message: dict[str, object] = response.json()
    return message


class TestHandshake:
    async def test_valid_ticket_gets_hello(self, live_server: LiveServer) -> None:
        user = await live_server.user()
        sock = await user.connect()

        hello = await sock.recv()

        assert hello["type"] == "hello"
        assert hello["id"] is None
        payload = hello["payload"]
        assert payload["user_id"] == user.id
        assert payload["heartbeat_interval_s"] == 25
        assert isinstance(payload["latest_event_id"], int)
        assert payload["server_time"].endswith("Z")
        await sock.close()

    async def test_invalid_ticket_is_closed_with_4001(self, live_server: LiveServer) -> None:
        user = await live_server.user()

        sock = await user.connect(ticket="not-a-real-ticket")

        assert await sock.close_code() == 4001

    async def test_reused_ticket_is_closed_with_4001(self, live_server: LiveServer) -> None:
        user = await live_server.user()
        ticket = await user.ticket()
        first = await user.connect(ticket=ticket)
        await first.recv()

        second = await user.connect(ticket=ticket)

        assert await second.close_code() == 4001
        await first.close()

    async def test_expired_ticket_is_closed_with_4001(
        self, live_server: LiveServer, test_database: DatabaseUnderTest
    ) -> None:
        user = await live_server.user()
        ticket = await user.ticket()
        with psycopg.connect(_libpq_dsn(test_database.owner_url), autocommit=True) as conn:
            conn.execute("UPDATE ws_tickets SET expires_at = now() - interval '1 second'")

        sock = await user.connect(ticket=ticket)

        assert await sock.close_code() == 4001

    async def test_ticket_from_a_revoked_session_is_refused(self, live_server: LiveServer) -> None:
        user = await live_server.user()
        ticket = await user.ticket()
        await live_server.http.post("/api/v1/auth/logout-all", headers=user.headers)

        sock = await user.connect(ticket=ticket)

        assert await sock.close_code() == 4001

    async def test_concurrent_connects_with_one_ticket_r7(self, live_server: LiveServer) -> None:
        user = await live_server.user()
        ticket = await user.ticket()

        socks = await asyncio.gather(*(user.connect(ticket=ticket) for _ in range(2)))
        outcomes = await asyncio.gather(*(s.recv(timeout=5) for s in socks), return_exceptions=True)

        hellos = [o for o in outcomes if isinstance(o, dict) and o["type"] == "hello"]
        assert len(hellos) == 1
        codes = [
            await s.close_code(timeout=2)
            for s, o in zip(socks, outcomes, strict=True)
            if o not in hellos
        ]
        assert codes == [4001]

    async def test_bad_origin_is_rejected(self, live_server: LiveServer) -> None:
        user = await live_server.user()

        with pytest.raises(InvalidStatus) as caught:
            await user.connect(origin="https://evil.example")

        assert caught.value.response.status_code == 403

    async def test_ticket_endpoint_requires_authentication(self, live_server: LiveServer) -> None:
        assert (await live_server.http.post("/api/v1/auth/ws-ticket")).status_code == 401

    async def test_tickets_never_appear_in_logs(
        self, live_server: LiveServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        user = await live_server.user()
        ticket = await user.ticket()
        sock = await user.connect(ticket=ticket)
        await sock.recv()
        await sock.close()
        await asyncio.sleep(0.1)

        # "websockets.client" is this test's own client library, not the server.
        for record in (r for r in caplog.records if not r.name.startswith("websockets.client")):
            assert ticket not in f"{record.getMessage()} {vars(record)}", record.name
            assert user.token not in f"{record.getMessage()} {vars(record)}", record.name
        ws_requests = [r for r in caplog.records if getattr(r, "event", None) == "ws.request"]
        assert ws_requests
        assert all(r.route == "/ws" for r in ws_requests)


class TestProtocol:
    async def test_ping_gets_pong_with_server_time(self, live_server: LiveServer) -> None:
        sock = await (await live_server.user()).online()

        await sock.send({"type": "ping", "ref": "c-1"})
        pong = await sock.recv()

        assert pong["type"] == "pong"
        assert pong["ref"] == "c-1"
        assert pong["payload"]["server_time"].endswith("Z")
        await sock.close()

    @pytest.mark.parametrize(
        ("frame", "code"),
        [
            ("not json at all", "invalid_json"),
            ('{"no_type": true}', "invalid_frame"),
            ('{"type": "message.send"}', "unknown_type"),
            ('{"type": "ping", "pad": "' + "x" * 17_000 + '"}', "frame_too_large"),
        ],
    )
    async def test_bad_frames_get_an_error_frame(
        self, live_server: LiveServer, frame: str, code: str
    ) -> None:
        sock = await (await live_server.user()).online()

        await sock.send(frame)
        error = await sock.recv()

        assert error["type"] == "error"
        assert error["payload"]["code"] == code
        await sock.close()

    async def test_a_flood_of_bad_frames_closes_with_4000(self, live_server: LiveServer) -> None:
        sock = await (await live_server.user()).online()

        for _ in range(12):
            await sock.send("garbage")

        assert await sock.close_code() == 4000


class TestIdle:
    @pytest.fixture
    def ws_settings_overrides(self) -> dict[str, object]:
        return {"ws_idle_timeout_seconds": 1.0}

    async def test_idle_socket_is_closed_with_1001(self, live_server: LiveServer) -> None:
        sock = await (await live_server.user()).online()

        assert await sock.close_code(timeout=5) == 1001


class TestFanOut:
    async def test_message_reaches_every_member_connection_and_no_one_else(
        self, live_server: LiveServer
    ) -> None:
        alice, bob, outsider = [await live_server.user() for _ in range(3)]
        cid = await group(live_server, alice, bob)
        alice_1, alice_2 = await alice.online(), await alice.online()
        bob_1, bob_2 = await bob.online(), await bob.online()
        stranger = await outsider.online()

        sent = await send(live_server, alice, cid, "hello team")

        for sock in (alice_1, alice_2, bob_1, bob_2):
            event = await sock.recv_type("message.created")
            assert set(event) == ENVELOPE_KEYS
            assert event["v"] == 1
            assert isinstance(event["id"], int)
            assert event["conversation_id"] == cid
            assert event["payload"] == sent  # the REST schema, exactly
            assert event["correlation_id"]
        await stranger.nothing_within(0.5)

    async def test_edit_and_delete_are_delivered(self, live_server: LiveServer) -> None:
        alice, bob = await live_server.user(), await live_server.user()
        cid = await group(live_server, alice, bob)
        bob_sock = await bob.online()
        sent = await send(live_server, alice, cid, "draft")
        await bob_sock.recv_type("message.created")

        await live_server.http.patch(
            f"/api/v1/messages/{sent['id']}", json={"body": "final"}, headers=alice.headers
        )
        updated = await bob_sock.recv_type("message.updated")
        await live_server.http.delete(f"/api/v1/messages/{sent['id']}", headers=alice.headers)
        deleted = await bob_sock.recv_type("message.deleted")

        assert updated["payload"]["body"] == "final"
        assert deleted["payload"]["id"] == sent["id"]
        assert updated["id"] < deleted["id"]  # global event order

    async def test_removed_member_is_told_and_then_gets_nothing(
        self, live_server: LiveServer
    ) -> None:
        alice, bob = await live_server.user(), await live_server.user()
        cid = await group(live_server, alice, bob)
        bob_sock = await bob.online()

        await live_server.http.delete(
            f"/api/v1/conversations/{cid}/members/{bob.id}", headers=alice.headers
        )
        removed = await bob_sock.recv_type("conversation.member_removed")
        await send(live_server, alice, cid, "after bob left")

        assert removed["payload"]["user_id"] == bob.id
        await bob_sock.nothing_within(0.5)

    async def test_conversation_created_reaches_both_sides(self, live_server: LiveServer) -> None:
        alice, bob = await live_server.user(), await live_server.user()
        bob_sock = await bob.online()

        await live_server.http.post(
            "/api/v1/conversations/direct", json={"user_id": bob.id}, headers=alice.headers
        )

        event = await bob_sock.recv_type("conversation.created")
        assert event["payload"]["type"] == "direct"


class TestSlowConsumer:
    # Default queue size (256): it must exceed the listener's delivery burst (100 ids
    # per batch), or even a healthy client would overflow during one burst.
    async def test_a_stalled_client_is_closed_4008_while_others_keep_receiving(
        self, live_server: LiveServer, test_database: DatabaseUnderTest
    ) -> None:
        stalled_user, healthy_user = await live_server.user(), await live_server.user()
        stalled_socket = await stalled_user.connect_and_stall()
        healthy = await healthy_user.online()
        manager = live_server.app.state.connections
        for _ in range(100):
            if manager.connections_for(UUID(stalled_user.id)):
                break
            await asyncio.sleep(0.02)
        [stalled] = manager.connections_for(UUID(stalled_user.id))
        events = 600  # ~39 MB: far more than socket buffers plus the 256-frame queue

        with psycopg.connect(_libpq_dsn(test_database.owner_url), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO event_outbox (type, recipient_user_ids, payload) "
                "SELECT 'test.bulk', ARRAY[%s::uuid, %s::uuid], "
                "jsonb_build_object('n', n, 'pad', repeat('x', 65536)) "
                "FROM generate_series(1, %s) AS n",
                (stalled_user.id, healthy_user.id, events),
            )

        received = 0
        while received < events:
            frame = await healthy.recv(timeout=30)
            if frame["type"] == "test.bulk":
                received += 1

        try:
            # The healthy client got everything; the stalled one was cut off, not waited on.
            assert received == events
            assert stalled.close_requested
            assert stalled.close_code == 4008
            assert stalled.frames_out < events
        finally:
            stalled_socket.close()
            await healthy.close()


async def test_notify_arrives_only_after_commit(test_database: DatabaseUnderTest) -> None:
    import asyncpg

    from app.realtime.outbox_listener import libpq_dsn

    notified: list[str] = []
    got_one = asyncio.Event()
    listener = await asyncpg.connect(libpq_dsn(test_database.app_url))
    await listener.add_listener("events", lambda *args: (notified.append(args[-1]), got_one.set()))
    writer = await asyncpg.connect(libpq_dsn(test_database.app_url))
    try:
        insert = (
            "INSERT INTO event_outbox (type, recipient_user_ids, payload) "
            "VALUES ('t', ARRAY[gen_random_uuid()], '{}') RETURNING id"
        )
        async with writer.transaction():
            # A rolled-back transaction must never notify.
            tx = writer.transaction()
            await tx.start()
            await writer.fetchval(insert)
            await tx.rollback()
        await asyncio.sleep(0.3)
        assert notified == []

        async with writer.transaction():
            event_id = await writer.fetchval(insert)
            await asyncio.sleep(0.2)
            assert notified == []  # not before commit
        await asyncio.wait_for(got_one.wait(), timeout=5)
        assert notified == [str(event_id)]
    finally:
        await writer.close()
        await listener.close()
