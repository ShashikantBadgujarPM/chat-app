from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request

LIMIT = 64 * 1024


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    # A route that reads the body, for exercising the streaming (no Content-Length) path.
    @app.post("/_test/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    return app


async def test_body_over_the_limit_is_rejected_with_413(client: httpx.AsyncClient) -> None:
    response = await client.post("/_test/echo", content=b"x" * (LIMIT + 1))

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "payload_too_large"
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_body_at_the_limit_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post("/_test/echo", content=b"x" * LIMIT)

    assert response.status_code == 200
    assert response.json() == {"size": LIMIT}


async def test_chunked_body_over_the_limit_is_rejected_with_413(
    client: httpx.AsyncClient,
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * (16 * 1024)

    # A streamed body is sent without Content-Length, so the byte count is enforced.
    response = await client.post("/_test/echo", content=chunks())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
