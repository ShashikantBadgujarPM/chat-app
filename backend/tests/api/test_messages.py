import asyncio
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest

from tests.api.auth_helpers import bearer, login_token, register


@pytest.fixture
def db_settings_overrides() -> dict[str, object]:
    return {"rate_limit_enabled": False}


@dataclass
class Chat:
    cid: str
    alice: dict[str, str]
    bob: dict[str, str]
    outsider: dict[str, str]
    former: dict[str, str]


@pytest.fixture
async def chat(db_client: httpx.AsyncClient) -> Chat:
    people = [await register(db_client) for _ in range(4)]
    tokens = [bearer(await login_token(db_client, p)) for p in people]
    alice, bob, outsider, former = tokens
    created = await db_client.post(
        "/api/v1/conversations/groups",
        json={"title": "Chat", "member_ids": [people[1].id, people[3].id]},
        headers=alice,
    )
    cid = created.json()["id"]
    await db_client.delete(f"/api/v1/conversations/{cid}/members/{people[3].id}", headers=former)
    return Chat(cid, alice, bob, outsider, former)


async def send(
    client: httpx.AsyncClient, chat: Chat, headers: dict[str, str], body: str, **extra: object
) -> httpx.Response:
    return await client.post(
        f"/api/v1/conversations/{chat.cid}/messages",
        json={
            "client_message_id": str(extra.pop("client_message_id", uuid4())),
            "body": body,
            **extra,
        },
        headers=headers,
    )


class TestSend:
    async def test_returns_the_message(self, db_client: httpx.AsyncClient, chat: Chat) -> None:
        response = await send(
            db_client, chat, chat.alice, "  Hey Rahul, please check the deployment.  "
        )

        assert response.status_code == 201
        body = response.json()
        assert body["body"] == "Hey Rahul, please check the deployment."  # trimmed
        assert body["seq"] == 1
        assert body["sender"]["display_name"]
        assert body["mentions"] == []
        assert body["created_at"].endswith("Z")
        assert body["edited_at"] is None

    async def test_same_client_message_id_is_idempotent_r5(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        client_id = uuid4()

        first = await send(db_client, chat, chat.alice, "original", client_message_id=client_id)
        replay = await send(db_client, chat, chat.alice, "original", client_message_id=client_id)
        different = await send(db_client, chat, chat.alice, "changed", client_message_id=client_id)

        assert first.status_code == 201
        assert replay.status_code == 200
        assert different.status_code == 200
        assert first.json() == replay.json()
        # Documented behavior: a replay returns the *original*, whatever the new body.
        assert different.json()["body"] == "original"
        assert different.json()["id"] == first.json()["id"]

    @pytest.mark.parametrize(
        ("body", "code"), [("   ", "body_empty"), ("x" * 4001, "body_too_long")]
    )
    async def test_body_rules(
        self, db_client: httpx.AsyncClient, chat: Chat, body: str, code: str
    ) -> None:
        response = await send(db_client, chat, chat.alice, body)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == code

    async def test_exactly_4000_characters_is_allowed(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        assert (await send(db_client, chat, chat.alice, "x" * 4000)).status_code == 201

    async def test_reply_in_the_same_conversation(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        parent = (await send(db_client, chat, chat.alice, "parent")).json()

        reply = await send(db_client, chat, chat.bob, "child", reply_to_id=parent["id"])

        assert reply.status_code == 201
        assert reply.json()["reply_to"] == {
            "id": parent["id"],
            "seq": parent["seq"],
            "sender_id": parent["sender"]["id"],
            "body_preview": "parent",
            "deleted": False,
        }

    async def test_reply_to_another_conversation_is_422(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        other = await db_client.post(
            "/api/v1/conversations/groups", json={"title": "Other"}, headers=chat.alice
        )
        foreign = await db_client.post(
            f"/api/v1/conversations/{other.json()['id']}/messages",
            json={"client_message_id": str(uuid4()), "body": "elsewhere"},
            headers=chat.alice,
        )

        response = await send(
            db_client, chat, chat.alice, "reply", reply_to_id=foreign.json()["id"]
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "reply_not_in_conversation"

    async def test_reply_to_a_deleted_message_shows_a_tombstone_preview(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        parent = (await send(db_client, chat, chat.alice, "soon gone")).json()
        await db_client.delete(f"/api/v1/messages/{parent['id']}", headers=chat.alice)

        reply = await send(db_client, chat, chat.bob, "replying anyway", reply_to_id=parent["id"])

        assert reply.status_code == 201
        assert reply.json()["reply_to"]["deleted"] is True
        assert reply.json()["reply_to"]["body_preview"] == ""


class TestEditDelete:
    async def test_sender_edits(self, db_client: httpx.AsyncClient, chat: Chat) -> None:
        message = (await send(db_client, chat, chat.alice, "first")).json()

        response = await db_client.patch(
            f"/api/v1/messages/{message['id']}", json={"body": " second "}, headers=chat.alice
        )

        assert response.status_code == 200
        assert response.json()["body"] == "second"
        assert response.json()["edited_at"] is not None

    async def test_delete_is_a_tombstone_and_idempotent(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        message = (await send(db_client, chat, chat.alice, "temporary")).json()

        first = await db_client.delete(f"/api/v1/messages/{message['id']}", headers=chat.alice)
        second = await db_client.delete(f"/api/v1/messages/{message['id']}", headers=chat.alice)
        fetched = await db_client.get(f"/api/v1/messages/{message['id']}", headers=chat.bob)

        assert (first.status_code, second.status_code) == (204, 204)
        assert fetched.json()["body"] is None
        assert fetched.json()["deleted_at"] is not None
        assert fetched.json()["seq"] == message["seq"]

    async def test_editing_a_deleted_message_is_409_r13(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        message = (await send(db_client, chat, chat.alice, "x")).json()
        await db_client.delete(f"/api/v1/messages/{message['id']}", headers=chat.alice)

        response = await db_client.patch(
            f"/api/v1/messages/{message['id']}", json={"body": "y"}, headers=chat.alice
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "message_deleted"

    async def test_concurrent_edit_and_delete_end_consistent_r13(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        message = (await send(db_client, chat, chat.alice, "x")).json()

        edit, delete = await asyncio.gather(
            db_client.patch(
                f"/api/v1/messages/{message['id']}", json={"body": "edited"}, headers=chat.alice
            ),
            db_client.delete(f"/api/v1/messages/{message['id']}", headers=chat.alice),
        )

        assert edit.status_code in (200, 409)
        assert delete.status_code == 204
        final = (
            await db_client.get(f"/api/v1/messages/{message['id']}", headers=chat.alice)
        ).json()
        # Either order ends as a tombstone: deletion always wins.
        assert final["body"] is None
        assert final["deleted_at"] is not None

    @pytest.mark.parametrize("method", ["PATCH", "DELETE"])
    async def test_non_sender_is_403(
        self, db_client: httpx.AsyncClient, chat: Chat, method: str
    ) -> None:
        message = (await send(db_client, chat, chat.alice, "mine")).json()

        response = await db_client.request(
            method,
            f"/api/v1/messages/{message['id']}",
            json={"body": "theirs"} if method == "PATCH" else None,
            headers=chat.bob,
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "not_sender"


class TestVisibility:
    @pytest.mark.parametrize("who", ["outsider", "former"])
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/api/v1/conversations/{cid}/messages", None),
            ("GET", "/api/v1/conversations/{cid}/messages/around/1", None),
            (
                "POST",
                "/api/v1/conversations/{cid}/messages",
                {"client_message_id": "{uuid}", "body": "hi"},
            ),
            ("GET", "/api/v1/messages/{mid}", None),
            ("PATCH", "/api/v1/messages/{mid}", {"body": "x"}),
            ("DELETE", "/api/v1/messages/{mid}", None),
        ],
    )
    async def test_non_members_and_former_members_get_404(
        self,
        db_client: httpx.AsyncClient,
        chat: Chat,
        who: str,
        method: str,
        path: str,
        body: dict[str, str] | None,
    ) -> None:
        message = (await send(db_client, chat, chat.alice, "private")).json()
        url = path.format(cid=chat.cid, mid=message["id"])
        payload = {k: v.format(uuid=uuid4()) for k, v in body.items()} if body is not None else None

        response = await db_client.request(method, url, json=payload, headers=getattr(chat, who))

        assert response.status_code == 404


class TestHistory:
    @pytest.fixture
    async def seven(self, db_client: httpx.AsyncClient, chat: Chat) -> Chat:
        for n in range(1, 8):
            await send(db_client, chat, chat.alice, f"m{n}")
        return chat

    async def page(self, client: httpx.AsyncClient, chat: Chat, query: str) -> httpx.Response:
        return await client.get(
            f"/api/v1/conversations/{chat.cid}/messages?{query}", headers=chat.bob
        )

    async def test_latest_then_before_down_to_seq_1(
        self, db_client: httpx.AsyncClient, seven: Chat
    ) -> None:
        seqs: list[int] = []
        latest = (await self.page(db_client, seven, "limit=3")).json()
        seqs += [m["seq"] for m in latest["items"]]
        has_more = latest["has_more"]
        while has_more:
            page = (await self.page(db_client, seven, f"limit=3&before_seq={seqs[-1]}")).json()
            seqs += [m["seq"] for m in page["items"]]
            has_more = page["has_more"]

        assert seqs == [7, 6, 5, 4, 3, 2, 1]

    async def test_after_is_oldest_first(self, db_client: httpx.AsyncClient, seven: Chat) -> None:
        page = (await self.page(db_client, seven, "after_seq=4&limit=10")).json()

        assert [m["seq"] for m in page["items"]] == [5, 6, 7]
        assert page["has_more"] is False

    async def test_around(self, db_client: httpx.AsyncClient, seven: Chat) -> None:
        response = await db_client.get(
            f"/api/v1/conversations/{seven.cid}/messages/around/4?limit=3", headers=seven.bob
        )

        body = response.json()
        assert [m["seq"] for m in body["items"]] == [3, 4, 5]
        assert body["has_more_before"] is True
        assert body["has_more_after"] is True

    async def test_both_cursors_is_400(self, db_client: httpx.AsyncClient, seven: Chat) -> None:
        response = await self.page(db_client, seven, "before_seq=5&after_seq=2")

        assert response.status_code == 400

    async def test_limit_over_100_is_422(self, db_client: httpx.AsyncClient, seven: Chat) -> None:
        assert (await self.page(db_client, seven, "limit=101")).status_code == 422

    async def test_conversation_list_shows_the_last_message(
        self, db_client: httpx.AsyncClient, seven: Chat
    ) -> None:
        listing = await db_client.get("/api/v1/conversations", headers=seven.bob)

        [conversation] = [c for c in listing.json()["items"] if c["id"] == seven.cid]
        assert conversation["last_message"]["seq"] == 7
        assert conversation["last_message"]["body_preview"] == "m7"
        assert conversation["last_message_seq"] == 7


async def test_sending_moves_the_conversation_to_the_top(
    db_client: httpx.AsyncClient, chat: Chat
) -> None:
    newer = await db_client.post(
        "/api/v1/conversations/groups", json={"title": "Newer"}, headers=chat.alice
    )
    await send(db_client, chat, chat.alice, "activity")

    listing = (await db_client.get("/api/v1/conversations", headers=chat.alice)).json()

    assert [c["id"] for c in listing["items"]][:2] == [chat.cid, newer.json()["id"]]
