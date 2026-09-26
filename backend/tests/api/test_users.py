from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.platform.pagination import encode_cursor
from tests.api.auth_helpers import (
    CSRF,
    RegisteredUser,
    bearer,
    login,
    login_token,
    refresh_cookie,
    register,
)
from tests.api.test_auth_register_login import set_user_status
from tests.harness import DatabaseUnderTest


@pytest.fixture
def db_settings_overrides() -> dict[str, object]:
    # Several tests register many users from one test client IP.
    return {"rate_limit_enabled": False}


async def signed_in(client: httpx.AsyncClient, **overrides: str) -> tuple[RegisteredUser, str]:
    user = await register(client, **overrides)
    return user, await login_token(client, user)


class TestMe:
    async def test_patch_changes_only_the_fields_given(self, db_client: httpx.AsyncClient) -> None:
        user, token = await signed_in(db_client, display_name="Before")

        renamed = await db_client.patch(
            "/api/v1/users/me", json={"display_name": "After"}, headers=bearer(token)
        )
        moved = await db_client.patch(
            "/api/v1/users/me", json={"timezone": "Europe/Berlin"}, headers=bearer(token)
        )

        assert renamed.status_code == 200
        assert renamed.json()["display_name"] == "After"
        assert renamed.json()["timezone"] == "UTC"
        assert moved.json() | {"display_name": "After", "timezone": "Europe/Berlin"} == moved.json()
        reloaded = await db_client.get("/api/v1/users/me", headers=bearer(token))
        assert reloaded.json()["display_name"] == "After"
        assert reloaded.json()["timezone"] == "Europe/Berlin"
        assert reloaded.json()["username"] == user.username

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"username": "renamed"}, "validation_error"),
            ({"display_name": ""}, "validation_error"),
            ({"timezone": "Not/AZone"}, "invalid_timezone"),
        ],
    )
    async def test_invalid_patches_are_rejected(
        self, db_client: httpx.AsyncClient, body: dict[str, str], code: str
    ) -> None:
        _, token = await signed_in(db_client)

        response = await db_client.patch("/api/v1/users/me", json=body, headers=bearer(token))

        assert response.status_code == 422
        assert response.json()["error"]["code"] == code


class TestDeleteMe:
    async def test_wrong_password_is_refused(self, db_client: httpx.AsyncClient) -> None:
        _, token = await signed_in(db_client)

        response = await db_client.request(
            "DELETE",
            "/api/v1/users/me",
            json={"password": "not my password"},
            headers=bearer(token),
        )

        assert response.status_code == 401
        assert (await db_client.get("/api/v1/users/me", headers=bearer(token))).status_code == 200

    async def test_deletes_revokes_sessions_and_frees_the_username(
        self, db_client: httpx.AsyncClient
    ) -> None:
        user = await register(db_client, username="leaving")
        first = await login(db_client, user)
        cookie = refresh_cookie(first)
        token = first.json()["access_token"]

        response = await db_client.request(
            "DELETE", "/api/v1/users/me", json={"password": user.password}, headers=bearer(token)
        )

        assert response.status_code == 204
        assert (await login(db_client, user)).status_code == 401
        assert (await db_client.get("/api/v1/users/me", headers=bearer(token))).status_code == 401
        db_client.cookies.clear()
        refreshed = await db_client.post(
            "/api/v1/auth/refresh", headers={**CSRF, "Cookie": f"rt={cookie}"}
        )
        assert refreshed.status_code == 401
        again = await register(db_client, username="leaving", email="new-owner@example.com")
        assert again.username == "leaving"


class TestPublicProfile:
    async def test_returns_the_public_projection(self, db_client: httpx.AsyncClient) -> None:
        other = await register(db_client, display_name="Other Person")
        _, token = await signed_in(db_client)

        response = await db_client.get(f"/api/v1/users/{other.id}", headers=bearer(token))

        assert response.status_code == 200
        assert response.json() == {
            "id": other.id,
            "username": other.username,
            "display_name": "Other Person",
            "presence": {"status": "offline", "last_seen_at": None},
        }

    async def test_unknown_and_disabled_users_are_404(
        self, db_client: httpx.AsyncClient, test_database: DatabaseUnderTest
    ) -> None:
        disabled = await register(db_client)
        await set_user_status(test_database, disabled.id, "disabled")
        _, token = await signed_in(db_client)

        for user_id in (str(uuid4()), disabled.id):
            response = await db_client.get(f"/api/v1/users/{user_id}", headers=bearer(token))
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "user_not_found"


class TestSearch:
    async def test_partial_username_and_display_name_match(
        self, db_client: httpx.AsyncClient
    ) -> None:
        await register(db_client, username="shashikant", display_name="S. Badgujar")
        await register(db_client, username="rahul_k", display_name="Rahul Kumar")
        _, token = await signed_in(db_client, username="searcher")

        by_username = await db_client.get("/api/v1/users?q=shas", headers=bearer(token))
        by_display = await db_client.get("/api/v1/users?q=kumar", headers=bearer(token))
        with_typo = await db_client.get("/api/v1/users?q=shashikat", headers=bearer(token))

        assert [u["username"] for u in by_username.json()["items"]] == ["shashikant"]
        assert [u["username"] for u in by_display.json()["items"]] == ["rahul_k"]
        assert "shashikant" in [u["username"] for u in with_typo.json()["items"]]

    async def test_excludes_the_caller_deleted_and_disabled_users(
        self, db_client: httpx.AsyncClient, test_database: DatabaseUnderTest
    ) -> None:
        disabled = await register(db_client, username="match_disabled")
        await set_user_status(test_database, disabled.id, "disabled")
        deleted = await register(db_client, username="match_deleted")
        deleted_token = await login_token(db_client, deleted)
        await db_client.request(
            "DELETE",
            "/api/v1/users/me",
            json={"password": deleted.password},
            headers=bearer(deleted_token),
        )
        await register(db_client, username="match_visible")
        _, token = await signed_in(db_client, username="match_caller")

        response = await db_client.get("/api/v1/users?q=match", headers=bearer(token))

        assert [u["username"] for u in response.json()["items"]] == ["match_visible"]

    async def test_query_under_two_characters_is_rejected(
        self, db_client: httpx.AsyncClient
    ) -> None:
        _, token = await signed_in(db_client)

        for q in ("a", " a "):
            response = await db_client.get(f"/api/v1/users?q={q}", headers=bearer(token))
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "query_too_short"

    async def test_like_wildcards_are_literal(self, db_client: httpx.AsyncClient) -> None:
        await register(db_client, username="plain_name")
        _, token = await signed_in(db_client, username="wildcard_caller")

        response = await db_client.get("/api/v1/users?q=%25%25", headers=bearer(token))

        assert response.json()["items"] == []

    async def test_pagination_is_stable_when_users_register_between_pages(
        self, db_client: httpx.AsyncClient
    ) -> None:
        for n in range(7):
            await register(db_client, username=f"paged_{n}")
        _, token = await signed_in(db_client, username="pager")

        first = await db_client.get("/api/v1/users?q=paged&limit=3", headers=bearer(token))
        await register(db_client, username="paged_late")  # arrives mid-pagination
        seen = [u["username"] for u in first.json()["items"]]
        cursor = first.json()["next_cursor"]
        while cursor:
            page = await db_client.get(
                f"/api/v1/users?q=paged&limit=3&cursor={cursor}", headers=bearer(token)
            )
            seen += [u["username"] for u in page.json()["items"]]
            cursor = page.json()["next_cursor"]

        originals = {f"paged_{n}" for n in range(7)}
        assert len(seen) == len(set(seen))  # no duplicates
        assert originals <= set(seen)  # no omissions

    async def test_tampered_cursor_is_400(self, db_client: httpx.AsyncClient) -> None:
        _, token = await signed_in(db_client)

        for cursor in ("garbage!!", encode_cursor(["not-a-number", "not-a-uuid"])):
            response = await db_client.get(
                f"/api/v1/users?q=ab&cursor={cursor}", headers=bearer(token)
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_cursor"

    async def test_requires_authentication(self, db_client: httpx.AsyncClient) -> None:
        assert (await db_client.get("/api/v1/users?q=abc")).status_code == 401


@pytest.mark.slow
async def test_search_uses_the_trigram_indexes(
    db_client: httpx.AsyncClient, test_database: DatabaseUnderTest
) -> None:
    """At a realistic size the planner serves every OR arm from the trigram indexes.

    Small tables can't show this: below tens of thousands of rows a sequential scan is
    genuinely cheaper, and the planner rightly picks it. So seed 100k users and ANALYZE
    (as the owner) first. Measured: seq scan at 5k rows, bitmap-OR of both trigram
    indexes at 100k.
    """
    from app.modules.identity.infrastructure.user_repository import _SEARCH
    from tests.harness import _libpq_dsn

    with psycopg.connect(_libpq_dsn(test_database.owner_url), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (username, email, display_name, password_hash) "
            "SELECT 'user_' || n, 'user_' || n || '@example.com', 'Person ' || md5(n::text), 'h' "
            "FROM generate_series(1, 100000) AS n"
        )
        conn.execute("ANALYZE users")

    engine = create_async_engine(test_database.app_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(f"EXPLAIN {_SEARCH.text}"),
                {
                    "q": "shas",
                    "pattern": "%shas%",
                    "exclude_id": uuid4(),
                    "after_score": None,
                    "after_id": None,
                    "limit": 21,
                },
            )
            plan = "\n".join(row[0] for row in rows)
    finally:
        await engine.dispose()

    assert "Seq Scan" not in plan, plan
    assert "ix_users_username_trgm" in plan, plan
    assert "ix_users_display_name_trgm" in plan, plan
