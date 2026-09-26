"""API tests against a real database: `db_app` and `db_client`.

The app under test uses its own engine against the per-worker test database, so its
transactions really commit, as in production (concurrency tests depend on that). The
tables are truncated after each test.
"""

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import FastAPI

from app.main import create_app
from app.platform.clock import FrozenClock
from tests.conftest import SettingsFactory
from tests.harness import DatabaseUnderTest, truncate_all_tables

# Cheap Argon2 parameters keep the suite fast; the event-loop test uses the defaults.
FAST_ARGON2 = {"argon2_time_cost": 1, "argon2_memory_cost_kib": 8, "argon2_parallelism": 1}


@pytest.fixture
def db_settings_overrides() -> dict[str, object]:
    """Override in a test module to change Settings for `db_app`."""
    return {}


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def db_app(
    make_settings: SettingsFactory,
    test_database: DatabaseUnderTest,
    db_settings_overrides: dict[str, object],
    clock: FrozenClock,
) -> Iterator[FastAPI]:
    settings = make_settings(
        database_url=test_database.app_url, **{**FAST_ARGON2, **db_settings_overrides}
    )
    app = create_app(settings)
    app.state.clock = clock
    try:
        yield app
    finally:
        truncate_all_tables(test_database)


@pytest.fixture
async def db_client(db_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # https: the refresh cookie is Secure, and httpx only sends it back over https.
    async with (
        db_app.router.lifespan_context(db_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=db_app), base_url="https://test"
        ) as client,
    ):
        yield client
