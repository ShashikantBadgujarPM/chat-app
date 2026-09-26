import os
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI

# `app.main` builds its module-level app at import time, which reads the environment.
# Give it safe test values before anything imports it. Real values always win.
os.environ.setdefault("ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-not-for-real-use-000000")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")

from app.config import Settings  # after the environment defaults above
from app.main import create_app

TEST_JWT_SECRET = "test-only-jwt-secret-not-for-real-use-000000"
# Nothing listens on port 1, so API tests without the integration harness see the
# database as unavailable: connection refused, fast.
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"

SettingsFactory = Callable[..., Settings]


@pytest.fixture(autouse=True)
def _isolate_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings read the process environment too. Inside a container that environment
    # is the app's real config (e.g. ENV=development), so remove it for every test.
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Build Settings from explicit values; keyword arguments override the defaults."""

    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "env": "test",
            "jwt_secret": TEST_JWT_SECRET,
            "database_url": UNREACHABLE_DATABASE_URL,
            "log_format": "json",
        }
        values.update(overrides)
        return Settings.model_validate(values)

    return factory


@pytest.fixture
def app(make_settings: SettingsFactory) -> FastAPI:
    return create_app(make_settings())


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # httpx's ASGITransport doesn't run the lifespan, so run it explicitly.
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c
