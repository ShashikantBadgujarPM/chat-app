import asyncio
import time
from uuid import uuid4

import httpx
import psycopg
import pytest

from tests.api.auth_helpers import bearer, login_token, register
from tests.harness import DatabaseUnderTest, _libpq_dsn


@pytest.fixture
def db_settings_overrides() -> dict[str, object]:
    return {"rate_limit_enabled": False}


async def people(client: httpx.AsyncClient, count: int) -> list[tuple[str, dict[str, str]]]:
    result = []
    for _ in range(count):
        user = await register(client)
        result.append((user.id, bearer(await login_token(client, user))))
    return result


async def send(client: httpx.AsyncClient, cid: str, headers: dict[str, str], body: str) -> int:
    response = await client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"client_message_id": str(uuid4()), "body": body},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    seq: int = response.json()["seq"]
    return seq


async def unread(client: httpx.AsyncClient, cid: str, headers: dict[str, str]) -> int:
    listing = (await client.get("/api/v1/conversations", headers=headers)).json()["items"]
    [conversation] = [c for c in listing if c["id"] == cid]
    count: int = conversation["unread_count"]
    return count


async def put_cursor(
    client: httpx.AsyncClient, cid: str, headers: dict[str, str], seq: int
) -> httpx.Response:
    return await client.put(
        f"/api/v1/conversations/{cid}/read-cursor", json={"last_read_seq": seq}, headers=headers
    )


@pytest.fixture
async def dm(db_client: httpx.AsyncClient) -> tuple[str, dict[str, str], dict[str, str]]:
    (_, alice), (bob_id, bob) = await people(db_client, 2)
    cid = (
        await db_client.post(
            "/api/v1/conversations/direct", json={"user_id": bob_id}, headers=alice
        )
    ).json()["id"]
    return cid, alice, bob


async def test_unread_ignores_own_and_deleted_messages_and_clears_on_read(
    db_client: httpx.AsyncClient, dm: tuple[str, dict[str, str], dict[str, str]]
) -> None:
    cid, alice, bob = dm
    await send(db_client, cid, alice, "one")
    second = await send(db_client, cid, alice, "two")
    await send(db_client, cid, bob, "bob's own message")
    third = (
        await db_client.post(
            f"/api/v1/conversations/{cid}/messages",
            json={"client_message_id": str(uuid4()), "body": "three"},
            headers=alice,
        )
    ).json()
    await db_client.delete(f"/api/v1/messages/{third['id']}", headers=alice)

    assert await unread(db_client, cid, bob) == 2  # own and deleted messages don't count
    response = await put_cursor(db_client, cid, bob, second)
    assert response.json() == {"last_read_seq": second, "unread_count": 0}
    assert await unread(db_client, cid, bob) == 0


async def test_messages_from_a_deleted_sender_still_count(
    db_client: httpx.AsyncClient,
    dm: tuple[str, dict[str, str], dict[str, str]],
    test_database: DatabaseUnderTest,
) -> None:
    cid, alice, bob = dm
    await send(db_client, cid, alice, "from alice")
    with psycopg.connect(_libpq_dsn(test_database.owner_url), autocommit=True) as conn:
        conn.execute("UPDATE messages SET sender_id = NULL")  # sender hard-deleted

    assert await unread(db_client, cid, bob) == 1


async def test_cursor_beyond_latest_is_422_and_non_member_is_404(
    db_client: httpx.AsyncClient, dm: tuple[str, dict[str, str], dict[str, str]]
) -> None:
    cid, alice, bob = dm
    await send(db_client, cid, alice, "one")
    [(_, outsider)] = await people(db_client, 1)

    beyond = await put_cursor(db_client, cid, bob, 2)
    stranger = await put_cursor(db_client, cid, outsider, 1)

    assert beyond.status_code == 422
    assert beyond.json()["error"]["code"] == "cursor_beyond_latest"
    assert stranger.status_code == 404


async def test_out_of_order_concurrent_cursors_end_at_the_highest_r10(
    db_client: httpx.AsyncClient, dm: tuple[str, dict[str, str], dict[str, str]]
) -> None:
    cid, alice, bob = dm
    for n in range(10):
        await send(db_client, cid, alice, f"m{n}")

    await asyncio.gather(*(put_cursor(db_client, cid, bob, seq) for seq in (10, 5, 8)))
    stale = await put_cursor(db_client, cid, bob, 3)

    assert stale.json()["last_read_seq"] == 10  # a stale tab can't move it back
    listing = (await db_client.get("/api/v1/conversations", headers=bob)).json()["items"]
    assert listing[0]["my_last_read_seq"] == 10


async def test_members_expose_read_positions_only_where_receipts_apply(
    db_client: httpx.AsyncClient, dm: tuple[str, dict[str, str], dict[str, str]]
) -> None:
    cid, alice, bob = dm
    seq = await send(db_client, cid, alice, "hi")
    await put_cursor(db_client, cid, bob, seq)

    members = (await db_client.get(f"/api/v1/conversations/{cid}/members", headers=alice)).json()

    assert {m["last_read_seq"] for m in members["items"]} == {0, seq}


@pytest.mark.slow
async def test_conversation_list_stays_fast_with_large_histories(
    db_client: httpx.AsyncClient, test_database: DatabaseUnderTest
) -> None:
    """50 conversations x 10k messages: GET /conversations stays well under a second.

    M07 says < 100 ms on the dev machine, "noted, not asserted in CI". The bound here is
    loose on purpose; the measured time is printed for the PR notes.
    """
    (me_id, me), (other_id, _) = await people(db_client, 2)
    with psycopg.connect(_libpq_dsn(test_database.owner_url), autocommit=True) as conn:
        conn.execute(
            """
            WITH groups AS (
                INSERT INTO conversations (type, title, last_message_seq)
                SELECT 'group', 'G' || g, 10000 FROM generate_series(1, 50) AS g
                RETURNING id
            ), members AS (
                INSERT INTO conversation_members (conversation_id, user_id)
                SELECT id, u FROM groups, unnest(ARRAY[%s::uuid, %s::uuid]) AS u
            )
            INSERT INTO messages (conversation_id, seq, sender_id, body, client_message_id)
            SELECT g.id, s, %s::uuid, 'message ' || s, gen_random_uuid()
            FROM groups AS g, generate_series(1, 10000) AS s
            """,
            (me_id, other_id, other_id),
        )
        conn.execute("ANALYZE")

    await db_client.get("/api/v1/conversations", headers=me)  # warm up
    started = time.perf_counter()
    response = await db_client.get("/api/v1/conversations?limit=50", headers=me)
    elapsed_ms = (time.perf_counter() - started) * 1000

    print(f"GET /conversations with 50 x 10k messages: {elapsed_ms:.0f} ms")
    assert response.status_code == 200
    assert all(c["unread_count"] == 10000 for c in response.json()["items"])
    assert elapsed_ms < 2000
