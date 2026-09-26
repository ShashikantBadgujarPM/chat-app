import asyncio
from datetime import timedelta

import httpx
import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.platform.clock import FrozenClock
from tests.api.auth_helpers import (
    CSRF,
    bearer,
    login,
    login_token,
    refresh_cookie,
    register,
)
from tests.api.test_auth_register_login import set_user_status
from tests.conftest import TEST_JWT_SECRET
from tests.harness import DatabaseUnderTest


async def refresh_with(client: httpx.AsyncClient, cookie: str) -> httpx.Response:
    # Send an explicit cookie, independent of the client's cookie jar.
    client.cookies.clear()
    return await client.post("/api/v1/auth/refresh", headers={**CSRF, "Cookie": f"rt={cookie}"})


async def audit_actions(database: DatabaseUnderTest) -> list[str]:
    engine = create_async_engine(database.app_url)
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT action FROM audit_logs ORDER BY id"))
        actions = [row[0] for row in rows]
    await engine.dispose()
    return actions


class TestRefresh:
    async def test_rotation_revokes_the_old_cookie_and_the_new_one_works(
        self, db_client: httpx.AsyncClient, clock: FrozenClock
    ) -> None:
        user = await register(db_client)
        first = refresh_cookie(await login(db_client, user))

        rotated = await refresh_with(db_client, first)
        assert rotated.status_code == 200
        second = refresh_cookie(rotated)
        assert second != first
        me = await db_client.get("/api/v1/users/me", headers=bearer(rotated.json()["access_token"]))
        assert me.status_code == 200

        clock.advance(timedelta(seconds=11))  # past the grace period
        assert (await refresh_with(db_client, second)).status_code == 200

    async def test_missing_csrf_header_is_forbidden(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)
        await login(db_client, user)

        response = await db_client.post("/api/v1/auth/refresh")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_header_missing"

    async def test_missing_or_unknown_cookie_is_401_and_clears_the_cookie(
        self, db_client: httpx.AsyncClient
    ) -> None:
        db_client.cookies.clear()
        missing = await db_client.post("/api/v1/auth/refresh", headers=CSRF)
        unknown = await refresh_with(db_client, "not-a-real-token")

        for response in (missing, unknown):
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_refresh_token"
            assert (
                'rt=""' in response.headers["set-cookie"]
                or "rt=;" in response.headers["set-cookie"]
            )

    async def test_expired_refresh_token_is_rejected(
        self, db_client: httpx.AsyncClient, clock: FrozenClock
    ) -> None:
        user = await register(db_client)
        cookie = refresh_cookie(await login(db_client, user))

        clock.advance(timedelta(days=14, seconds=1))

        response = await refresh_with(db_client, cookie)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_refresh_token"

    async def test_reuse_after_the_grace_period_revokes_the_whole_family(
        self,
        db_client: httpx.AsyncClient,
        clock: FrozenClock,
        test_database: DatabaseUnderTest,
    ) -> None:
        user = await register(db_client)
        old = refresh_cookie(await login(db_client, user))
        new = refresh_cookie(await refresh_with(db_client, old))

        clock.advance(timedelta(seconds=11))
        replay = await refresh_with(db_client, old)

        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "refresh_token_reused"
        # The legitimate successor is revoked too: attacker and victim both sign in again.
        after = await refresh_with(db_client, new)
        assert after.status_code == 401
        assert "auth.refresh_reuse_detected" in await audit_actions(test_database)

    async def test_reuse_only_revokes_that_family(
        self, db_client: httpx.AsyncClient, clock: FrozenClock
    ) -> None:
        user = await register(db_client)
        other_session = refresh_cookie(await login(db_client, user))
        old = refresh_cookie(await login(db_client, user))
        await refresh_with(db_client, old)

        clock.advance(timedelta(seconds=11))
        await refresh_with(db_client, old)

        assert (await refresh_with(db_client, other_session)).status_code == 200

    async def test_within_the_grace_period_it_is_superseded_not_reused(
        self, db_client: httpx.AsyncClient
    ) -> None:
        user = await register(db_client)
        old = refresh_cookie(await login(db_client, user))
        new = refresh_cookie(await refresh_with(db_client, old))

        again = await refresh_with(db_client, old)

        assert again.status_code == 409
        assert again.json()["error"]["code"] == "refresh_superseded"
        assert "set-cookie" not in again.headers
        assert (await refresh_with(db_client, new)).status_code == 200  # family intact

    async def test_concurrent_refreshes_with_the_same_cookie_r6(
        self, db_client: httpx.AsyncClient
    ) -> None:
        user = await register(db_client)
        cookie = refresh_cookie(await login(db_client, user))
        db_client.cookies.clear()

        responses = await asyncio.gather(
            *(
                db_client.post("/api/v1/auth/refresh", headers={**CSRF, "Cookie": f"rt={cookie}"})
                for _ in range(2)
            )
        )

        assert sorted(r.status_code for r in responses) == [200, 409]
        [winner] = [r for r in responses if r.status_code == 200]
        # The family was not revoked: the winner's successor still works.
        assert (await refresh_with(db_client, refresh_cookie(winner))).status_code == 200


class TestLogout:
    async def test_logout_revokes_the_session_and_clears_the_cookie(
        self, db_client: httpx.AsyncClient
    ) -> None:
        user = await register(db_client)
        cookie = refresh_cookie(await login(db_client, user))

        response = await db_client.post(
            "/api/v1/auth/logout", headers={**CSRF, "Cookie": f"rt={cookie}"}
        )

        assert response.status_code == 204
        assert "rt=" in response.headers["set-cookie"]
        assert (await refresh_with(db_client, cookie)).status_code == 401

    async def test_logout_is_idempotent(self, db_client: httpx.AsyncClient) -> None:
        db_client.cookies.clear()
        assert (await db_client.post("/api/v1/auth/logout", headers=CSRF)).status_code == 204
        response = await db_client.post(
            "/api/v1/auth/logout", headers={**CSRF, "Cookie": "rt=unknown"}
        )
        assert response.status_code == 204

    async def test_logout_requires_the_csrf_header(self, db_client: httpx.AsyncClient) -> None:
        assert (await db_client.post("/api/v1/auth/logout")).status_code == 403

    async def test_logout_all_revokes_every_session(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)
        first = refresh_cookie(await login(db_client, user))
        second_login = await login(db_client, user)
        second = refresh_cookie(second_login)

        response = await db_client.post(
            "/api/v1/auth/logout-all", headers=bearer(second_login.json()["access_token"])
        )

        assert response.status_code == 204
        assert (await refresh_with(db_client, first)).status_code == 401
        assert (await refresh_with(db_client, second)).status_code == 401


class TestSessions:
    async def test_lists_active_sessions_and_marks_the_current_one(
        self, db_client: httpx.AsyncClient
    ) -> None:
        user = await register(db_client)
        await login(db_client, user)
        current = await login(db_client, user)

        response = await db_client.get(
            "/api/v1/auth/sessions",
            headers={**bearer(current.json()["access_token"]), "User-Agent": "pytest"},
        )

        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 2
        assert sum(item["current"] for item in items) == 1
        assert all(item["created_at"].endswith("Z") for item in items)
        assert {item["ip_address"] for item in items} == {"127.0.0.1"}

    async def test_revoke_one_session(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)
        other = refresh_cookie(await login(db_client, user))
        token = await login_token(db_client, user)
        sessions = (await db_client.get("/api/v1/auth/sessions", headers=bearer(token))).json()
        [other_session] = [s for s in sessions["items"] if not s["current"]]

        response = await db_client.delete(
            f"/api/v1/auth/sessions/{other_session['family_id']}", headers=bearer(token)
        )

        assert response.status_code == 204
        assert (await refresh_with(db_client, other)).status_code == 401

    async def test_cannot_revoke_another_users_session(self, db_client: httpx.AsyncClient) -> None:
        alice = await register(db_client)
        bob = await register(db_client)
        alice_token = await login_token(db_client, alice)
        bob_token = await login_token(db_client, bob)
        [bob_session] = (
            await db_client.get("/api/v1/auth/sessions", headers=bearer(bob_token))
        ).json()["items"]

        response = await db_client.delete(
            f"/api/v1/auth/sessions/{bob_session['family_id']}", headers=bearer(alice_token)
        )

        assert response.status_code == 404


class TestChangePassword:
    async def test_revokes_other_sessions_and_keeps_this_one(
        self, db_client: httpx.AsyncClient
    ) -> None:
        user = await register(db_client)
        other = refresh_cookie(await login(db_client, user))
        this_login = await login(db_client, user)
        this = refresh_cookie(this_login)
        new_password = "a brand new passphrase"

        response = await db_client.post(
            "/api/v1/auth/password",
            json={"current_password": user.password, "new_password": new_password},
            headers=bearer(this_login.json()["access_token"]),
        )

        assert response.status_code == 204
        assert (await refresh_with(db_client, other)).status_code == 401
        assert (await refresh_with(db_client, this)).status_code == 200
        assert (await login(db_client, user)).status_code == 401
        assert (await login(db_client, user, password=new_password)).status_code == 200

    async def test_wrong_current_password(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)
        token = await login_token(db_client, user)

        response = await db_client.post(
            "/api/v1/auth/password",
            json={"current_password": "not my password!", "new_password": "a brand new one!"},
            headers=bearer(token),
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_weak_new_password(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)
        token = await login_token(db_client, user)

        response = await db_client.post(
            "/api/v1/auth/password",
            json={"current_password": user.password, "new_password": "short"},
            headers=bearer(token),
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "weak_password"


class TestAccessTokens:
    async def test_missing_header(self, db_client: httpx.AsyncClient) -> None:
        response = await db_client.get("/api/v1/users/me")

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    async def test_expired_token(self, db_client: httpx.AsyncClient, clock: FrozenClock) -> None:
        user = await register(db_client)
        token = await login_token(db_client, user)

        clock.advance(timedelta(seconds=901))

        response = await db_client.get("/api/v1/users/me", headers=bearer(token))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "token_expired"

    async def test_bad_signature(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)
        claims = jwt.decode(
            await login_token(db_client, user),
            TEST_JWT_SECRET,
            algorithms=["HS256"],
            audience="chat-app-api",
            options={"verify_exp": False, "verify_iat": False},
        )
        forged = jwt.encode(claims, "an attacker's secret that is long enough!", algorithm="HS256")

        response = await db_client.get("/api/v1/users/me", headers=bearer(forged))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_token"

    async def test_refresh_token_is_not_an_access_token(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)
        cookie = refresh_cookie(await login(db_client, user))

        response = await db_client.get("/api/v1/users/me", headers=bearer(cookie))
        assert response.status_code == 401

    async def test_disabled_user_is_rejected_immediately(
        self, db_client: httpx.AsyncClient, test_database: DatabaseUnderTest
    ) -> None:
        user = await register(db_client)
        token = await login_token(db_client, user)

        await set_user_status(test_database, user.id, "disabled")

        assert (await db_client.get("/api/v1/users/me", headers=bearer(token))).status_code == 401


class TestAudit:
    async def test_security_events_are_recorded(
        self,
        db_client: httpx.AsyncClient,
        test_database: DatabaseUnderTest,
    ) -> None:
        user = await register(db_client)
        for _ in range(5):
            await login(db_client, user, password="wrong password!!")

        actions = await audit_actions(test_database)
        assert actions.count("auth.login_failed") == 5
        assert "auth.account_locked" in actions

    async def test_audit_rows_are_append_only_for_the_app_role(
        self, db_client: httpx.AsyncClient, test_database: DatabaseUnderTest
    ) -> None:
        user = await register(db_client)
        await login(db_client, user, password="wrong password!!")

        engine = create_async_engine(test_database.app_url)
        try:
            async with engine.begin() as conn:
                with pytest.raises(Exception, match="permission denied"):
                    await conn.execute(text("DELETE FROM audit_logs"))
        finally:
            await engine.dispose()


class TestRateLimits:
    @pytest.fixture
    def db_settings_overrides(self) -> dict[str, object]:
        return {"rate_limit_login_per_minute": 3}

    async def test_login_is_limited_per_ip(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)

        statuses = [(await login(db_client, user)).status_code for _ in range(4)]

        assert statuses == [200, 200, 200, 429]
        limited = await login(db_client, user)
        assert limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) >= 1
