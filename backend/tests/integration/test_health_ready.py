import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.main import create_app
from tests.conftest import SettingsFactory
from tests.harness import DatabaseUnderTest


@pytest.fixture
def app(make_settings: SettingsFactory, test_database: DatabaseUnderTest) -> FastAPI:
    return create_app(make_settings(database_url=test_database.app_url))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def test_ready_returns_200_when_the_database_and_listener_are_up(
    client: httpx.AsyncClient,
) -> None:
    # The listener connects in the background after startup.
    for _ in range(100):
        response = await client.get("/health/ready")
        if response.status_code == 200:
            break
        await asyncio.sleep(0.05)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "outbox_listener": "ok"},
    }
