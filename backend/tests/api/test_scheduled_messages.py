"""/api/v1/scheduled-messages against a real database (docs/design/07, M11)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.platform.clock import FrozenClock
from app.platform.db import UnitOfWork
from app.worker import Worker
from tests.api.auth_helpers import bearer, login_token, register

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
LATER = "2026-10-01T18:00:00+05:30"  # 12:30Z


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def db_settings_overrides() -> dict[str, object]:
    return {"rate_limit_enabled": False, "schedule_max_pending_per_user": 3}


@dataclass
class Chat:
    cid: str
    alice: dict[str, str]
    bob: dict[str, str]
    outsider: dict[str, str]
    bob_id: str


@pytest.fixture
async def chat(db_client: httpx.AsyncClient) -> Chat:
    people = [await register(db_client) for _ in range(3)]
    alice, bob, outsider = [bearer(await login_token(db_client, p)) for p in people]
    created = await db_client.post(
        "/api/v1/conversations/groups",
        json={"title": "Ops", "member_ids": [people[1].id]},
        headers=alice,
    )
    return Chat(created.json()["id"], alice, bob, outsider, people[1].id)


async def schedule(
    client: httpx.AsyncClient, chat: Chat, headers: dict[str, str] | None = None, **fields: Any
) -> httpx.Response:
    body: dict[str, Any] = {
        "client_message_id": str(uuid4()),
        "conversation_id": chat.cid,
        "body": "Hey Rahul, please check the deployment.",
        "scheduled_at": LATER,
        "timezone": "Asia/Kolkata",
    }
    body.update(fields)
    return await client.post("/api/v1/scheduled-messages", json=body, headers=headers or chat.alice)


async def make_due(
    app: FastAPI, scheduled_id: str, *, ago: timedelta = timedelta(seconds=1)
) -> None:
    async with UnitOfWork(app.state.session_factory) as uow:
        await uow.session.execute(
            text(
                "UPDATE scheduled_messages SET scheduled_at_utc = now() - CAST(:ago AS interval),"
                " next_attempt_at = now() - CAST(:ago AS interval) WHERE id = :id"
            ),
            {"ago": ago, "id": scheduled_id},
        )


async def run_worker_batch(app: FastAPI) -> int:
    return await Worker(app.state.settings, app.state.session_factory, housekeeping=[]).run_batch()


class TestCreate:
    async def test_stores_utc_and_keeps_the_timezone(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        response = await schedule(db_client, chat)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["scheduled_at"] == "2026-10-01T12:30:00Z"
        assert body["timezone"] == "Asia/Kolkata"
        assert body["status"] == "pending"
        assert body["attempts"] == 0
        assert body["sent_message_id"] is None

    async def test_replay_returns_the_original_with_200(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        client_id = str(uuid4())
        first = await schedule(db_client, chat, client_message_id=client_id)
        again = await schedule(db_client, chat, client_message_id=client_id, body="different")

        assert again.status_code == 200
        assert again.json()["id"] == first.json()["id"]
        assert again.json()["body"] == first.json()["body"]

    async def test_timezone_defaults_to_the_profile(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        response = await schedule(db_client, chat, timezone=None)
        assert response.json()["timezone"] == "UTC"

    @pytest.mark.parametrize(
        ("fields", "code"),
        [
            ({"scheduled_at": "2026-10-01T18:00:00"}, "naive_datetime"),
            ({"scheduled_at": "2026-09-26T12:00:20Z"}, "scheduled_in_past"),
            ({"scheduled_at": "2027-09-27T12:00:00Z"}, "scheduled_too_far"),
            ({"timezone": "Mars/Olympus"}, "invalid_timezone"),
            ({"body": "   "}, "body_empty"),
        ],
    )
    async def test_invalid_input_is_422(
        self, db_client: httpx.AsyncClient, chat: Chat, fields: dict[str, Any], code: str
    ) -> None:
        response = await schedule(db_client, chat, **fields)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == code

    async def test_non_member_gets_404(self, db_client: httpx.AsyncClient, chat: Chat) -> None:
        response = await schedule(db_client, chat, chat.outsider)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "conversation_not_found"

    async def test_pending_limit_is_429(self, db_client: httpx.AsyncClient, chat: Chat) -> None:
        for _ in range(3):
            assert (await schedule(db_client, chat)).status_code == 201
        response = await schedule(db_client, chat)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "too_many_pending"

    async def test_reply_must_be_in_the_same_conversation(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        response = await schedule(db_client, chat, reply_to_id=str(uuid4()))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "reply_not_in_conversation"


class TestReadAndList:
    async def test_other_users_rows_are_404(self, db_client: httpx.AsyncClient, chat: Chat) -> None:
        sid = (await schedule(db_client, chat)).json()["id"]
        response = await db_client.get(f"/api/v1/scheduled-messages/{sid}", headers=chat.bob)
        assert response.status_code == 404

    async def test_list_is_own_ordered_filtered_and_paginated(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        late = (await schedule(db_client, chat, scheduled_at="2026-10-03T00:00:00Z")).json()
        early = (await schedule(db_client, chat, scheduled_at="2026-10-02T00:00:00Z")).json()
        await schedule(db_client, chat, chat.bob)
        cancelled = (await schedule(db_client, chat, scheduled_at="2026-10-04T00:00:00Z")).json()
        await db_client.post(
            f"/api/v1/scheduled-messages/{cancelled['id']}/cancel", headers=chat.alice
        )

        first = await db_client.get(
            "/api/v1/scheduled-messages?status=pending&limit=1", headers=chat.alice
        )
        cursor = first.json()["next_cursor"]
        second = await db_client.get(
            f"/api/v1/scheduled-messages?status=pending&limit=1&cursor={cursor}",
            headers=chat.alice,
        )

        assert [i["id"] for i in first.json()["items"]] == [early["id"]]
        assert [i["id"] for i in second.json()["items"]] == [late["id"]]
        assert second.json()["next_cursor"] is None
        everything = await db_client.get(
            f"/api/v1/scheduled-messages?conversation_id={chat.cid}", headers=chat.alice
        )
        assert len(everything.json()["items"]) == 3


class TestEditCancelRetry:
    async def test_edit_changes_body_and_time(
        self, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        sid = (await schedule(db_client, chat)).json()["id"]
        response = await db_client.patch(
            f"/api/v1/scheduled-messages/{sid}",
            json={"body": "New text", "scheduled_at": "2026-10-02T09:00:00+01:00"},
            headers=chat.alice,
        )
        assert response.status_code == 200, response.text
        assert response.json()["body"] == "New text"
        assert response.json()["scheduled_at"] == "2026-10-02T08:00:00Z"

    async def test_cancel_is_idempotent(self, db_client: httpx.AsyncClient, chat: Chat) -> None:
        sid = (await schedule(db_client, chat)).json()["id"]
        url = f"/api/v1/scheduled-messages/{sid}/cancel"
        first = await db_client.post(url, headers=chat.alice)
        again = await db_client.post(url, headers=chat.alice)

        assert first.status_code == again.status_code == 200
        assert again.json()["status"] == "cancelled"
        edit = await db_client.patch(
            f"/api/v1/scheduled-messages/{sid}", json={"body": "x"}, headers=chat.alice
        )
        assert edit.status_code == 409
        assert edit.json()["error"]["code"] == "not_pending"

    async def test_retry_only_from_failed(self, db_client: httpx.AsyncClient, chat: Chat) -> None:
        sid = (await schedule(db_client, chat)).json()["id"]
        response = await db_client.post(
            f"/api/v1/scheduled-messages/{sid}/retry", headers=chat.alice
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_failed"


class TestDelivery:
    async def test_worker_sends_it_and_the_row_links_the_message(
        self, db_app: FastAPI, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        sid = (await schedule(db_client, chat)).json()["id"]
        await make_due(db_app, sid)

        assert await run_worker_batch(db_app) == 1

        row = (await db_client.get(f"/api/v1/scheduled-messages/{sid}", headers=chat.alice)).json()
        assert row["status"] == "sent"
        assert row["attempts"] == 1
        assert row["sent_at"] is not None
        history = await db_client.get(
            f"/api/v1/conversations/{chat.cid}/messages", headers=chat.bob
        )
        [message] = history.json()["items"]
        assert message["id"] == row["sent_message_id"]
        assert message["scheduled_message_id"] == sid
        assert message["body"] == "Hey Rahul, please check the deployment."

        # Sent: no more changes.
        edit = await db_client.patch(
            f"/api/v1/scheduled-messages/{sid}", json={"body": "x"}, headers=chat.alice
        )
        cancel = await db_client.post(
            f"/api/v1/scheduled-messages/{sid}/cancel", headers=chat.alice
        )
        assert edit.status_code == cancel.status_code == 409

    async def test_removed_sender_fails_and_can_retry_after_rejoining(
        self, db_app: FastAPI, db_client: httpx.AsyncClient, chat: Chat
    ) -> None:
        sid = (await schedule(db_client, chat, chat.bob)).json()["id"]
        await db_client.delete(
            f"/api/v1/conversations/{chat.cid}/members/{chat.bob_id}", headers=chat.alice
        )
        await make_due(db_app, sid)
        await run_worker_batch(db_app)

        row = (await db_client.get(f"/api/v1/scheduled-messages/{sid}", headers=chat.bob)).json()
        assert row["status"] == "failed"
        assert row["last_error"] == "sender_not_member"

        retried = await db_client.post(
            f"/api/v1/scheduled-messages/{sid}/retry",
            json={"scheduled_at": (NOW + timedelta(hours=1)).isoformat()},
            headers=chat.bob,
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "pending"
        assert retried.json()["attempts"] == 0
        assert retried.json()["last_error"] is None
