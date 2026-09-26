import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from tests.conftest import SettingsFactory

LEAKED_DETAIL = "internal detail that must not leak"


@pytest.mark.parametrize(
    ("kind", "status", "code", "headers"),
    [
        ("domain", 422, "domain_error", {}),
        ("validation", 422, "validation_error", {}),
        ("conflict", 409, "not_pending", {}),
        ("invariant", 409, "cannot_remove_last_owner", {}),
        ("not_found", 404, "conversation_not_found", {}),
        ("authentication", 401, "invalid_token", {"www-authenticate": "Bearer"}),
        ("account_locked", 423, "account_locked", {"retry-after": "900"}),
        ("authorization", 403, "not_owner", {}),
        ("rate_limited", 429, "rate_limited", {"retry-after": "30"}),
    ],
)
async def test_each_app_error_maps_to_its_status_and_envelope(
    client: httpx.AsyncClient, kind: str, status: int, code: str, headers: dict[str, str]
) -> None:
    response = await client.get(f"/api/v1/_debug/error/{kind}")

    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
    for name, value in headers.items():
        assert response.headers[name] == value


async def test_account_locked_has_no_authentication_challenge(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/_debug/error/account_locked")

    assert "www-authenticate" not in response.headers


async def test_validation_details_are_passed_through(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/_debug/error/validation")

    assert response.json()["error"]["details"] == [{"field": "body", "issue": "too long"}]


async def test_request_validation_error_uses_the_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/_debug/error/not-a-kind")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    [detail] = error["details"]
    assert detail["field"] == "path.kind"
    assert detail["issue"]


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/does-not-exist", 404, "not_found"),
        ("POST", "/health/live", 405, "method_not_allowed"),
    ],
)
async def test_framework_http_errors_use_the_envelope(
    client: httpx.AsyncClient, method: str, path: str, status: int, code: str
) -> None:
    response = await client.request(method, path)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("kind", ["unhandled", "app"])
async def test_server_errors_return_a_generic_500_and_log_the_stack_trace(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    caplog.set_level(logging.ERROR)

    response = await client.get(f"/api/v1/_debug/error/{kind}")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error == {
        "code": "internal_error",
        "message": "An unexpected error occurred.",
        "details": None,
        "request_id": response.headers["X-Request-ID"],
    }
    assert LEAKED_DETAIL not in response.text
    assert "Traceback" not in response.text

    [logged] = [r for r in caplog.records if r.exc_info]
    assert logged.levelno == logging.ERROR
    assert LEAKED_DETAIL in str(logged.exc_info[1])  # type: ignore[index]


async def test_access_log_records_500_for_unhandled_errors(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.access")

    await client.get("/api/v1/_debug/error/unhandled")

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "http.request"]
    assert record.status == 500
    assert record.levelno == logging.ERROR


@pytest.fixture
async def dev_client(make_settings: SettingsFactory) -> AsyncIterator[httpx.AsyncClient]:
    app = create_dev_app(make_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def create_dev_app(make_settings: SettingsFactory) -> FastAPI:
    from app.main import create_app

    return create_app(make_settings(env="development"))


async def test_debug_route_does_not_exist_outside_the_test_environment(
    dev_client: httpx.AsyncClient,
) -> None:
    response = await dev_client.get("/api/v1/_debug/error/conflict")

    assert response.status_code == 404
