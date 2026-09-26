import asyncio
import logging
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.platform.clock import FrozenClock
from tests.api.auth_helpers import PASSWORD, bearer, login, register
from tests.harness import DatabaseUnderTest


class TestRegister:
    async def test_returns_user_me(self, db_client: httpx.AsyncClient) -> None:
        response = await db_client.post(
            "/api/v1/auth/register",
            json={
                "username": "alice",
                "email": "alice@example.com",
                "display_name": "Alice",
                "password": PASSWORD,
                "timezone": "Asia/Kolkata",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert set(body) == {"id", "username", "email", "display_name", "timezone", "created_at"}
        assert body["timezone"] == "Asia/Kolkata"
        assert body["created_at"].endswith("Z")

    @pytest.mark.parametrize(
        ("first", "second", "code"),
        [
            ({"username": "Bob"}, {"username": "bob"}, "username_taken"),
            ({"email": "bob@example.com"}, {"email": "BOB@example.com"}, "email_taken"),
        ],
    )
    async def test_duplicates_are_rejected_case_insensitively(
        self,
        db_client: httpx.AsyncClient,
        first: dict[str, str],
        second: dict[str, str],
        code: str,
    ) -> None:
        await register(db_client, **first)
        response = await db_client.post(
            "/api/v1/auth/register",
            json={
                "username": second.get("username", "other_user"),
                "email": second.get("email", "other@example.com"),
                "display_name": "Other",
                "password": PASSWORD,
            },
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == code

    @pytest.mark.parametrize("password", ["short", "password123"])
    async def test_weak_password_is_rejected(
        self, db_client: httpx.AsyncClient, password: str
    ) -> None:
        response = await db_client.post(
            "/api/v1/auth/register",
            json={
                "username": "weak",
                "email": "weak@example.com",
                "display_name": "Weak",
                "password": password,
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "weak_password"

    @pytest.mark.parametrize(
        "body_change",
        [
            {"username": "no spaces allowed"},
            {"username": "ab"},
            {"email": "not-an-email"},
            {"display_name": "   "},
            {"unexpected": "field"},
        ],
    )
    async def test_invalid_input_is_rejected(
        self, db_client: httpx.AsyncClient, body_change: dict[str, str]
    ) -> None:
        body = {
            "username": "valid_name",
            "email": "valid@example.com",
            "display_name": "Valid",
            "password": PASSWORD,
            **body_change,
        }
        response = await db_client.post("/api/v1/auth/register", json=body)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    async def test_unknown_timezone_is_rejected(self, db_client: httpx.AsyncClient) -> None:
        response = await db_client.post(
            "/api/v1/auth/register",
            json={
                "username": "tz_user",
                "email": "tz@example.com",
                "display_name": "TZ",
                "password": PASSWORD,
                "timezone": "Mars/Olympus",
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_timezone"


class TestLogin:
    async def test_success_returns_tokens_user_and_cookie(
        self, db_client: httpx.AsyncClient
    ) -> None:
        user = await register(db_client)

        response = await login(db_client, user)

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 900
        assert body["user"]["username"] == user.username
        me = await db_client.get("/api/v1/users/me", headers=bearer(body["access_token"]))
        assert me.status_code == 200
        assert me.json()["id"] == user.id

    async def test_login_by_email_is_case_insensitive(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client, email="mixed@example.com")

        response = await db_client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "MIXED@example.com", "password": user.password},
        )

        assert response.status_code == 200

    async def test_refresh_cookie_attributes(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)

        response = await login(db_client, user)

        [cookie] = response.headers.get_list("set-cookie")
        attributes = [part.strip().lower() for part in cookie.split(";")]
        assert attributes[0].startswith("rt=")
        assert "httponly" in attributes
        assert "secure" in attributes
        assert "samesite=strict" in attributes
        assert "path=/api/v1/auth" in attributes
        assert "max-age=1209600" in attributes  # 14 days

    async def test_unknown_user_and_wrong_password_look_the_same(
        self, db_client: httpx.AsyncClient
    ) -> None:
        user = await register(db_client)

        wrong_password = await login(db_client, user, password="definitely wrong password")
        unknown_user = await db_client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "nobody_here", "password": "definitely wrong password"},
        )

        for response in (wrong_password, unknown_user):
            assert response.status_code == 401
            error = response.json()["error"]
            assert error["code"] == "invalid_credentials"
        assert wrong_password.json()["error"]["message"] == unknown_user.json()["error"]["message"]

    async def test_disabled_user_cannot_log_in(
        self, db_client: httpx.AsyncClient, test_database: DatabaseUnderTest
    ) -> None:
        user = await register(db_client)
        await set_user_status(test_database, user.id, "disabled")

        response = await login(db_client, user)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"


class TestLockout:
    async def test_five_failures_lock_the_account_with_retry_after(
        self, db_client: httpx.AsyncClient
    ) -> None:
        user = await register(db_client)

        statuses = [
            (await login(db_client, user, password="wrong password!!")).status_code
            for _ in range(5)
        ]
        locked = await login(db_client, user)  # even the right password is refused

        assert statuses == [401, 401, 401, 401, 423]
        assert locked.status_code == 423
        assert locked.json()["error"]["code"] == "account_locked"
        assert 0 < int(locked.headers["Retry-After"]) <= 15 * 60

    async def test_account_unlocks_after_the_window(
        self, db_client: httpx.AsyncClient, clock: FrozenClock
    ) -> None:
        user = await register(db_client)
        for _ in range(5):
            await login(db_client, user, password="wrong password!!")

        clock.advance(timedelta(minutes=15, seconds=1))
        response = await login(db_client, user)

        assert response.status_code == 200

    async def test_success_resets_the_counter(self, db_client: httpx.AsyncClient) -> None:
        user = await register(db_client)
        for _ in range(4):
            await login(db_client, user, password="wrong password!!")
        assert (await login(db_client, user)).status_code == 200

        for _ in range(4):
            await login(db_client, user, password="wrong password!!")
        assert (await login(db_client, user)).status_code == 200


class GatedHasher:
    """Holds every verify() until `parties` of them are in flight, so all concurrent
    logins pass the lock check before any failure is recorded (deterministic, no sleep)."""

    def __init__(self, inner: object, parties: int) -> None:
        self._inner = inner
        self._barrier = asyncio.Barrier(parties)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def verify(self, password_hash: str, password: str) -> bool:
        await self._barrier.wait()
        result: bool = await self._inner.verify(password_hash, password)  # type: ignore[attr-defined]
        return result


@pytest.fixture
def db_settings_overrides() -> dict[str, object]:
    return {"rate_limit_enabled": False}


async def test_concurrent_failures_are_all_counted_r18(
    db_client: httpx.AsyncClient, db_app: FastAPI, test_database: DatabaseUnderTest
) -> None:
    user = await register(db_client)
    db_app.state.password_hasher = GatedHasher(db_app.state.password_hasher, parties=10)

    responses = await asyncio.gather(
        *(login(db_client, user, password="wrong password!!") for _ in range(10))
    )

    assert {r.status_code for r in responses} <= {401, 423}
    attempts, locked = await fetch_lock_state(test_database, user.id)
    assert attempts == 10  # no lost updates
    assert locked


async def test_hashing_does_not_block_the_event_loop(
    make_settings: object, db_client: httpx.AsyncClient, db_app: FastAPI
) -> None:
    from app.platform.security import Argon2PasswordHasher

    # Production-strength parameters for this one test.
    real = Argon2PasswordHasher(time_cost=3, memory_cost_kib=65536, parallelism=4)
    user = await register(db_client)
    started = asyncio.Event()

    class SignallingHasher:
        dummy_hash = real.dummy_hash

        def needs_rehash(self, password_hash: str) -> bool:
            return False

        async def hash(self, password: str) -> str:
            return await real.hash(password)

        async def verify(self, password_hash: str, password: str) -> bool:
            started.set()
            return await real.verify(real.dummy_hash, password)

    db_app.state.password_hasher = SignallingHasher()
    login_task = asyncio.create_task(login(db_client, user))
    await started.wait()

    loop = asyncio.get_running_loop()
    before = loop.time()
    health = await db_client.get("/health/live")
    elapsed = loop.time() - before

    await login_task
    assert health.status_code == 200
    assert elapsed < 0.05, f"/health/live took {elapsed * 1000:.1f} ms during a login"


async def test_login_and_refresh_never_log_secrets(
    db_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user = await register(db_client, password="a-very-distinct-password-9876")

    response = await login(db_client, user)
    access_token = response.json()["access_token"]
    refresh = response.cookies["rt"]
    refreshed = await db_client.post(
        "/api/v1/auth/refresh", headers={"X-Requested-With": "chat-app"}
    )
    await login(db_client, user, password="another-distinct-wrong-pw")

    secrets_seen = [user.password, access_token, refresh, refreshed.cookies["rt"]]
    for record in caplog.records:
        rendered = f"{record.getMessage()} {vars(record)}"
        for secret in secrets_seen:
            assert secret not in rendered, f"secret leaked in {record.name}: {record.msg}"


async def set_user_status(database: DatabaseUnderTest, user_id: str, status: str) -> None:
    engine = create_async_engine(database.app_url)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET status = :status WHERE id = :id"),
            {"status": status, "id": user_id},
        )
    await engine.dispose()


async def fetch_lock_state(database: DatabaseUnderTest, user_id: str) -> tuple[int, bool]:
    engine = create_async_engine(database.app_url)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT failed_login_attempts, locked_until IS NOT NULL "
                    "FROM users WHERE id = :id"
                ),
                {"id": user_id},
            )
        ).one()
    await engine.dispose()
    return int(row[0]), bool(row[1])
