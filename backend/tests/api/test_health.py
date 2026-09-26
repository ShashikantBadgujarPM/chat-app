import httpx
import pytest
from fastapi import FastAPI

from app.main import create_app
from app.platform.health import register_readiness_check
from tests.conftest import SettingsFactory


async def test_live_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_returns_503_when_the_database_is_unreachable(
    client: httpx.AsyncClient,
) -> None:
    # The 200 case needs a real database: tests/integration/test_health_ready.py.
    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"checks": {"database": "unavailable"}}


@pytest.fixture
def app_with_failing_check(app: FastAPI) -> FastAPI:
    async def failing_check() -> None:
        raise ConnectionError("dependency down: host=10.0.0.5")

    async def passing_check() -> None:
        return None

    register_readiness_check(app, "failing", failing_check)
    register_readiness_check(app, "passing", passing_check)
    return app


async def test_ready_returns_503_when_a_check_fails(
    app_with_failing_check: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "not_ready"
    checks = error["details"]["checks"]
    assert checks["failing"] == "unavailable"
    assert checks["passing"] == "ok"
    assert error["request_id"] == response.headers["X-Request-ID"]
    # The failure reason is logged, not returned.
    assert "10.0.0.5" not in response.text


async def test_live_ignores_failing_readiness_checks(
    app_with_failing_check: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200


@pytest.mark.parametrize(("env", "docs_status"), [("production", 404), ("development", 200)])
async def test_openapi_docs_are_disabled_in_production(
    make_settings: SettingsFactory, env: str, docs_status: int
) -> None:
    app = create_app(make_settings(env=env))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/docs")).status_code == docs_status
        assert (await client.get("/openapi.json")).status_code == docs_status
