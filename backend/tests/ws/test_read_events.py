"""conversation.read and receipt.updated delivery (M07)."""

from uuid import uuid4

import pytest

from tests.ws.conftest import LiveServer, WsUser

pytestmark = pytest.mark.slow


async def send(server: LiveServer, user: WsUser, cid: str) -> int:
    response = await server.http.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"client_message_id": str(uuid4()), "body": "hi"},
        headers=user.headers,
    )
    seq: int = response.json()["seq"]
    return seq


async def read(server: LiveServer, user: WsUser, cid: str, seq: int) -> None:
    response = await server.http.put(
        f"/api/v1/conversations/{cid}/read-cursor",
        json={"last_read_seq": seq},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text


async def test_reading_in_one_tab_syncs_the_badge_in_the_other(live_server: LiveServer) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    cid = (
        await live_server.http.post(
            "/api/v1/conversations/direct", json={"user_id": bob.id}, headers=alice.headers
        )
    ).json()["id"]
    seq = await send(live_server, alice, cid)
    bob_tab_2 = await bob.online()
    alice_tab = await alice.online()

    await read(live_server, bob, cid, seq)

    synced = await bob_tab_2.recv_type("conversation.read")
    assert synced["payload"] == {
        "conversation_id": cid,
        "last_read_seq": seq,
        "unread_count": 0,
        "unread_mention_count": 0,
    }
    receipt = await alice_tab.recv_type("receipt.updated")
    assert receipt["payload"] == {"conversation_id": cid, "user_id": bob.id, "last_read_seq": seq}


async def test_large_groups_get_no_receipts(live_server: LiveServer) -> None:
    owner = await live_server.user()
    members = [await live_server.user() for _ in range(24)]  # 25 with the owner
    cid = (
        await live_server.http.post(
            "/api/v1/conversations/groups",
            json={"title": "Big", "member_ids": [m.id for m in members]},
            headers=owner.headers,
        )
    ).json()["id"]
    seq = await send(live_server, owner, cid)
    owner_tab = await owner.online()

    await read(live_server, members[0], cid, seq)

    await owner_tab.nothing_within(0.5)


async def test_a_stale_cursor_publishes_nothing(live_server: LiveServer) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    cid = (
        await live_server.http.post(
            "/api/v1/conversations/direct", json={"user_id": bob.id}, headers=alice.headers
        )
    ).json()["id"]
    seq = await send(live_server, alice, cid)
    await read(live_server, bob, cid, seq)
    bob_tab = await bob.online()

    await read(live_server, bob, cid, seq)  # no movement

    await bob_tab.nothing_within(0.5)
