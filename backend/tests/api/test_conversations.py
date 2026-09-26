from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.api.auth_helpers import RegisteredUser, bearer, login_token, register
from tests.harness import DatabaseUnderTest


@pytest.fixture
def db_settings_overrides() -> dict[str, object]:
    return {"rate_limit_enabled": False}


@dataclass
class Actor:
    user: RegisteredUser
    headers: dict[str, str]


async def actor(client: httpx.AsyncClient, **overrides: str) -> Actor:
    user = await register(client, **overrides)
    return Actor(user=user, headers=bearer(await login_token(client, user)))


@dataclass
class GroupWorld:
    conversation_id: str
    owner: Actor
    member: Actor
    target: Actor  # another member, the object of removals
    outsider: Actor
    former: Actor
    newcomer: Actor  # never a member; used as the user to add


@pytest.fixture
async def world(db_client: httpx.AsyncClient) -> GroupWorld:
    owner, member, target, outsider, former, newcomer = [await actor(db_client) for _ in range(6)]
    created = await db_client.post(
        "/api/v1/conversations/groups",
        json={
            "title": "Team",
            "member_ids": [member.user.id, target.user.id, former.user.id],
        },
        headers=owner.headers,
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    left = await db_client.delete(
        f"/api/v1/conversations/{cid}/members/{former.user.id}", headers=former.headers
    )
    assert left.status_code == 204
    return GroupWorld(cid, owner, member, target, outsider, former, newcomer)


# The authorization policy for conversation endpoints (docs/design/06 §12, 07).
# One row per endpoint; one expected status per role.
ROLES = ("outsider", "former", "member", "owner")
POLICY = [
    # (name, method, path template, body, {role: status})
    ("get", "GET", "/{cid}", None, (404, 404, 200, 200)),
    ("rename", "PATCH", "/{cid}", {"title": "Renamed"}, (404, 404, 403, 200)),
    ("list members", "GET", "/{cid}/members", None, (404, 404, 200, 200)),
    ("add member", "POST", "/{cid}/members", {"user_ids": ["{newcomer}"]}, (404, 404, 403, 200)),
    ("remove member", "DELETE", "/{cid}/members/{target}", None, (404, 404, 403, 204)),
    ("leave", "DELETE", "/{cid}/members/{self}", None, (404, 404, 204, 409)),
    ("mute", "PATCH", "/{cid}/membership", {"notifications_muted": True}, (404, 404, 200, 200)),
]
MATRIX = [
    pytest.param(method, path, body, role, expected, id=f"{name}-{role}")
    for name, method, path, body, statuses in POLICY
    for role, expected in zip(ROLES, statuses, strict=True)
]


@pytest.mark.parametrize(("method", "path", "body", "role", "expected"), MATRIX)
async def test_authorization_matrix(
    db_client: httpx.AsyncClient,
    world: GroupWorld,
    method: str,
    path: str,
    body: dict[str, object] | None,
    role: str,
    expected: int,
) -> None:
    caller: Actor = getattr(world, role)
    names = {
        "cid": world.conversation_id,
        "target": world.target.user.id,
        "newcomer": world.newcomer.user.id,
        "self": caller.user.id,
    }

    def fill(value: object) -> object:
        if isinstance(value, str):
            return value.format(**names)
        if isinstance(value, list):
            return [fill(v) for v in value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value

    response = await db_client.request(
        method,
        "/api/v1/conversations" + str(fill(path)),
        json=fill(body) if body is not None else None,
        headers=caller.headers,
    )

    assert response.status_code == expected, response.text
    if expected == 404:
        assert response.json()["error"]["code"] == "conversation_not_found"


class TestDirect:
    async def test_either_side_lands_in_the_same_conversation(
        self, db_client: httpx.AsyncClient
    ) -> None:
        alice, bob = await actor(db_client), await actor(db_client)

        first = await db_client.post(
            "/api/v1/conversations/direct", json={"user_id": bob.user.id}, headers=alice.headers
        )
        again = await db_client.post(
            "/api/v1/conversations/direct", json={"user_id": bob.user.id}, headers=alice.headers
        )
        reverse = await db_client.post(
            "/api/v1/conversations/direct", json={"user_id": alice.user.id}, headers=bob.headers
        )

        assert first.status_code == 201
        assert again.status_code == 200
        assert reverse.status_code == 200
        assert first.json()["id"] == again.json()["id"] == reverse.json()["id"]
        body = first.json()
        assert body["type"] == "direct"
        assert body["title"] is None
        assert body["member_count"] == 2
        # The other member first, so the client can render the DM's name from it.
        assert body["members_preview"][0]["id"] == bob.user.id

    async def test_dm_with_yourself_is_422(self, db_client: httpx.AsyncClient) -> None:
        alice = await actor(db_client)

        response = await db_client.post(
            "/api/v1/conversations/direct", json={"user_id": alice.user.id}, headers=alice.headers
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "cannot_dm_self"

    async def test_dm_with_a_missing_or_deleted_user_is_404(
        self, db_client: httpx.AsyncClient
    ) -> None:
        alice = await actor(db_client)
        gone = await actor(db_client)
        await db_client.request(
            "DELETE",
            "/api/v1/users/me",
            json={"password": gone.user.password},
            headers=gone.headers,
        )

        for user_id in (str(uuid4()), gone.user.id):
            response = await db_client.post(
                "/api/v1/conversations/direct", json={"user_id": user_id}, headers=alice.headers
            )
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "user_not_found"

    async def test_direct_conversations_have_no_member_management(
        self, db_client: httpx.AsyncClient
    ) -> None:
        alice, bob = await actor(db_client), await actor(db_client)
        cid = (
            await db_client.post(
                "/api/v1/conversations/direct", json={"user_id": bob.user.id}, headers=alice.headers
            )
        ).json()["id"]

        rename = await db_client.patch(
            f"/api/v1/conversations/{cid}", json={"title": "x"}, headers=alice.headers
        )
        leave = await db_client.delete(
            f"/api/v1/conversations/{cid}/members/{alice.user.id}", headers=alice.headers
        )

        for response in (rename, leave):
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "not_a_group"


class TestGroups:
    @pytest.mark.parametrize("title", ["", "   ", "x" * 101])
    async def test_invalid_titles_are_422(self, db_client: httpx.AsyncClient, title: str) -> None:
        alice = await actor(db_client)

        response = await db_client.post(
            "/api/v1/conversations/groups", json={"title": title}, headers=alice.headers
        )

        assert response.status_code == 422

    async def test_unknown_members_are_listed(self, db_client: httpx.AsyncClient) -> None:
        alice = await actor(db_client)
        missing = str(uuid4())

        response = await db_client.post(
            "/api/v1/conversations/groups",
            json={"title": "G", "member_ids": [missing]},
            headers=alice.headers,
        )

        assert response.status_code == 404
        assert response.json()["error"]["details"] == {"user_ids": [missing]}

    async def test_creator_is_the_owner(self, db_client: httpx.AsyncClient) -> None:
        alice, bob = await actor(db_client), await actor(db_client)

        response = await db_client.post(
            "/api/v1/conversations/groups",
            json={"title": "Team", "member_ids": [bob.user.id]},
            headers=alice.headers,
        )
        members = await db_client.get(
            f"/api/v1/conversations/{response.json()['id']}/members", headers=bob.headers
        )

        assert response.json()["my_role"] == "owner"
        assert response.json()["member_count"] == 2
        roles = {m["user"]["id"]: m["role"] for m in members.json()["items"]}
        assert roles == {alice.user.id: "owner", bob.user.id: "member"}

    async def test_adding_reports_already_members(
        self, db_client: httpx.AsyncClient, world: GroupWorld
    ) -> None:
        response = await db_client.post(
            f"/api/v1/conversations/{world.conversation_id}/members",
            json={"user_ids": [world.member.user.id, world.newcomer.user.id]},
            headers=world.owner.headers,
        )

        assert response.status_code == 200
        assert [m["user"]["id"] for m in response.json()["added"]] == [world.newcomer.user.id]
        assert response.json()["already_members"] == [world.member.user.id]

    async def test_adding_past_the_member_limit_is_422(
        self, db_client: httpx.AsyncClient, world: GroupWorld, test_database: DatabaseUnderTest
    ) -> None:
        # Fill the group to exactly 100 active members (3 + 97) directly in the database.
        engine = create_async_engine(test_database.app_url)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "WITH fillers AS ("
                    "  INSERT INTO users (username, email, display_name, password_hash) "
                    "  SELECT 'filler_' || n, 'filler_' || n || '@example.com', 'F', 'h' "
                    "  FROM generate_series(1, 97) AS n RETURNING id) "
                    "INSERT INTO conversation_members (conversation_id, user_id) "
                    "SELECT :cid, id FROM fillers"
                ),
                {"cid": world.conversation_id},
            )
        await engine.dispose()

        response = await db_client.post(
            f"/api/v1/conversations/{world.conversation_id}/members",
            json={"user_ids": [world.newcomer.user.id]},
            headers=world.owner.headers,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "member_limit_exceeded"

    async def test_removed_member_loses_access(
        self, db_client: httpx.AsyncClient, world: GroupWorld
    ) -> None:
        removed = await db_client.delete(
            f"/api/v1/conversations/{world.conversation_id}/members/{world.target.user.id}",
            headers=world.owner.headers,
        )
        after = await db_client.get(
            f"/api/v1/conversations/{world.conversation_id}", headers=world.target.headers
        )

        assert removed.status_code == 204
        assert after.status_code == 404


class TestList:
    async def test_lists_only_active_memberships_newest_first(
        self, db_client: httpx.AsyncClient, world: GroupWorld
    ) -> None:
        second = await db_client.post(
            "/api/v1/conversations/groups", json={"title": "Second"}, headers=world.member.headers
        )

        member_list = await db_client.get("/api/v1/conversations", headers=world.member.headers)
        former_list = await db_client.get("/api/v1/conversations", headers=world.former.headers)

        items = member_list.json()["items"]
        assert [c["id"] for c in items] == [second.json()["id"], world.conversation_id]
        assert items[0]["last_message"] is None
        assert items[0]["unread_count"] == 0
        assert items[0]["last_activity_at"].endswith("Z")
        assert former_list.json()["items"] == []

    async def test_pagination(self, db_client: httpx.AsyncClient) -> None:
        alice = await actor(db_client)
        created = [
            (
                await db_client.post(
                    "/api/v1/conversations/groups", json={"title": f"G{n}"}, headers=alice.headers
                )
            ).json()["id"]
            for n in range(5)
        ]

        seen: list[str] = []
        cursor = None
        while True:
            url = "/api/v1/conversations?limit=2" + (f"&cursor={cursor}" if cursor else "")
            page = (await db_client.get(url, headers=alice.headers)).json()
            seen += [c["id"] for c in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break

        assert seen == list(reversed(created))
