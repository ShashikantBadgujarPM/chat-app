"""Presence and typing over real sockets (M08)."""


import psycopg
import pytest

from tests.harness import DatabaseUnderTest, _libpq_dsn
from tests.ws.conftest import LiveServer, WsUser

pytestmark = pytest.mark.slow


@pytest.fixture
def ws_settings_overrides() -> dict[str, object]:
    return {"presence_grace_seconds": 0.3}


async def dm(server: LiveServer, a: WsUser, b: WsUser) -> str:
    response = await server.http.post(
        "/api/v1/conversations/direct", json={"user_id": b.id}, headers=a.headers
    )
    cid: str = response.json()["id"]
    return cid


async def presence(server: LiveServer, viewer: WsUser, user: WsUser) -> dict[str, object]:
    response = await server.http.get(
        f"/api/v1/users/presence?ids={user.id}", headers=viewer.headers
    )
    [item] = response.json()["items"]
    result: dict[str, object] = item
    return result


def table_counts(database: DatabaseUnderTest) -> dict[str, int]:
    with psycopg.connect(_libpq_dsn(database.owner_url)) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename NOT IN ('alembic_version', 'user_presence', 'ws_tickets')"
            )
        ]
        return {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}  # type: ignore[index]  # noqa: S608


async def test_co_members_see_online_and_strangers_see_nothing(live_server: LiveServer) -> None:
    alice, bob, stranger = [await live_server.user() for _ in range(3)]
    await dm(live_server, alice, bob)
    bob_tab = await bob.online()
    stranger_tab = await stranger.online()

    alice_tab = await alice.online()

    event = await bob_tab.recv_type("presence.updated")
    assert event["id"] is None
    assert event["payload"]["user_id"] == alice.id
    assert event["payload"]["status"] == "online"
    await stranger_tab.nothing_within(0.5)
    assert (await presence(live_server, bob, alice))["status"] == "online"
    await alice_tab.close()


async def test_offline_only_after_the_last_tab_and_the_grace_period(
    live_server: LiveServer,
) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    await dm(live_server, alice, bob)
    tabs = [await alice.online() for _ in range(3)]
    bob_tab = await bob.online()

    await tabs[0].close()
    await tabs[1].close()
    await bob_tab.nothing_within(0.6)  # two tabs remain: still online
    await tabs[2].close()

    event = await bob_tab.recv_type("presence.updated", timeout=3)
    assert event["payload"]["status"] == "offline"
    assert event["payload"]["last_seen_at"]
    seen = await presence(live_server, bob, alice)
    assert seen["status"] == "offline"
    assert seen["last_seen_at"]


async def test_reconnecting_within_the_grace_period_does_not_flap_r8(
    live_server: LiveServer,
) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    await dm(live_server, alice, bob)
    alice_tab = await alice.online()
    bob_tab = await bob.online()

    await alice_tab.close()
    alice_again = await alice.online()  # a reload

    await bob_tab.none_of_type("presence.updated", 0.8)
    assert (await presence(live_server, bob, alice))["status"] == "online"
    await alice_again.close()


async def test_typing_reaches_the_others_and_touches_no_table(
    live_server: LiveServer, test_database: DatabaseUnderTest
) -> None:
    alice, bob = await live_server.user(), await live_server.user()
    cid = await dm(live_server, alice, bob)
    alice_1, alice_2 = await alice.online(), await alice.online()
    bob_tab = await bob.online()
    before = table_counts(test_database)

    for _ in range(10):
        await alice_1.send({"type": "typing.start", "payload": {"conversation_id": cid}})
    typing = await bob_tab.recv_type("typing.updated")
    await bob_tab.none_of_type("typing.updated", 0.5)  # the throttle: one of the ten

    assert typing["id"] is None
    assert typing["payload"] == {
        "conversation_id": cid,
        "user_id": alice.id,
        "is_typing": True,
        "expires_in_s": 6,
    }
    await alice_2.none_of_type("typing.updated", 0.3)  # not echoed to the sender
    assert table_counts(test_database) == before  # nothing persisted


async def test_typing_into_a_foreign_conversation_is_an_error(live_server: LiveServer) -> None:
    alice, bob, stranger = [await live_server.user() for _ in range(3)]
    cid = await dm(live_server, alice, bob)
    sock = await stranger.online()

    await sock.send({"type": "typing.start", "ref": "t-1", "payload": {"conversation_id": cid}})
    error = await sock.recv_type("error")

    assert error["ref"] == "t-1"
    assert error["payload"]["code"] == "not_a_member"


async def test_startup_marks_everyone_offline(
    live_server: LiveServer, test_database: DatabaseUnderTest
) -> None:
    from app.modules.presence.infrastructure.store import SqlPresenceStore

    user = await live_server.user()
    with psycopg.connect(_libpq_dsn(test_database.owner_url), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO user_presence (user_id, status) VALUES (%s, 'online') "
            "ON CONFLICT (user_id) DO UPDATE SET status = 'online'",
            (user.id,),
        )

    await SqlPresenceStore(live_server.app.state.session_factory).reset_all_offline()

    with psycopg.connect(_libpq_dsn(test_database.owner_url)) as conn:
        status = conn.execute(
            "SELECT status FROM user_presence WHERE user_id = %s", (user.id,)
        ).fetchone()
    assert status == ("offline",)
