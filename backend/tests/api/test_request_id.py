import io
import json
import logging
from collections.abc import Iterator

import httpx
import pytest


async def test_valid_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "client-req.0001"})

    assert response.headers["X-Request-ID"] == "client-req.0001"


@pytest.mark.parametrize(
    "incoming",
    [
        "short",  # under 8 characters
        "x" * 65,  # over 64 characters
        "has spaces in it",
        'inject"}{"level":"CRITICAL',
    ],
)
async def test_invalid_request_id_is_replaced(client: httpx.AsyncClient, incoming: str) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": incoming})

    generated = response.headers["X-Request-ID"]
    assert generated != incoming
    assert len(generated) == 32  # uuid4().hex


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/does-not-exist"),
        ("POST", "/health/live"),
        ("GET", "/api/v1/_debug/error/conflict"),
        ("GET", "/api/v1/_debug/error/unhandled"),
    ],
)
async def test_every_response_has_a_request_id(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path)

    assert response.headers.get("X-Request-ID")


@pytest.fixture
def log_output(client: httpx.AsyncClient) -> Iterator[io.StringIO]:
    """Redirect the app's real stdout handler (filters + formatter included) to a buffer."""
    [handler] = logging.getLogger().handlers
    assert isinstance(handler, logging.StreamHandler)
    buffer = io.StringIO()
    original = handler.setStream(buffer)
    try:
        yield buffer
    finally:
        if original is not None:
            handler.setStream(original)


async def test_access_log_is_one_json_line_with_request_id_route_status_and_duration(
    client: httpx.AsyncClient, log_output: io.StringIO
) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "trace-abc-123"})

    lines = [json.loads(line) for line in log_output.getvalue().splitlines()]
    [entry] = [line for line in lines if line["event"] == "http.request"]
    assert entry["request_id"] == response.headers["X-Request-ID"] == "trace-abc-123"
    assert entry["route"] == "/health/live"
    assert entry["method"] == "GET"
    assert entry["status"] == 200
    assert isinstance(entry["duration_ms"], float)
    assert entry["service"] == "api"


async def test_access_log_uses_the_route_template(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.access")

    await client.get("/api/v1/_debug/error/not_found")

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "http.request"]
    assert record.route == "/api/v1/_debug/error/{kind}"
    assert record.status == 404
