"""A real uvicorn server for WebSocket tests (docs/design/11 §21.2).

The server runs in the test's own event loop on an ephemeral port, with the real
lifespan: engine, outbox listener, connection manager. Clients use the `websockets`
library over real sockets, so backpressure and concurrency behave as in production.
"""

import asyncio
import base64
import json
import os
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection, connect

from app.main import create_app
from tests.api.conftest import FAST_ARGON2
from tests.conftest import SettingsFactory
from tests.harness import DatabaseUnderTest, truncate_all_tables

ORIGIN = "http://localhost:5173"
PASSWORD = "correct horse battery staple"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class LiveServer:
    app: FastAPI
    http: httpx.AsyncClient
    ws_base: str
    _counter: list[int] = field(default_factory=lambda: [0])

    async def user(self, **overrides: str) -> "WsUser":
        self._counter[0] += 1
        n = f"{self._counter[0]}_{uuid4().hex[:6]}"
        body = {
            "username": f"ws_{n}",
            "email": f"ws_{n}@example.com",
            "display_name": f"WS {n}",
            "password": PASSWORD,
            **overrides,
        }
        registered = await self.http.post("/api/v1/auth/register", json=body)
        assert registered.status_code == 201, registered.text
        login = await self.http.post(
            "/api/v1/auth/login",
            json={"username_or_email": body["username"], "password": PASSWORD},
        )
        return WsUser(self, registered.json()["id"], login.json()["access_token"])


@dataclass
class WsUser:
    server: LiveServer
    id: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def ticket(self) -> str:
        response = await self.server.http.post("/api/v1/auth/ws-ticket", headers=self.headers)
        assert response.status_code == 201, response.text
        ticket: str = response.json()["ticket"]
        return ticket

    async def connect(self, *, ticket: str | None = None, origin: str | None = ORIGIN) -> "Socket":
        ticket = ticket or await self.ticket()
        ws = await connect(
            f"{self.server.ws_base}/ws?ticket={ticket}",
            origin=origin,  # type: ignore[arg-type]
            max_size=None,
        )
        return Socket(ws)

    async def connect_and_stall(self) -> socket.socket:
        """A peer that completes the WebSocket handshake and then never reads again.

        A client library can't do this: its transport keeps reading bytes into its own
        buffers. A raw socket with a tiny receive window is a genuinely stuck peer.
        """
        ticket = await self.ticket()
        host, port = self.server.ws_base.removeprefix("ws://").split(":")
        key = base64.b64encode(os.urandom(16)).decode()
        crlf = "\r\n"
        request = crlf.join(
            [
                f"GET /ws?ticket={ticket} HTTP/1.1",
                f"Host: {host}:{port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                f"Origin: {ORIGIN}",
                "",
                "",
            ]
        ).encode()

        def handshake() -> socket.socket:
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.connect((host, int(port)))
            sock.sendall(request)
            response = b""
            while b"\r\n\r\n" not in response:
                response += sock.recv(1)  # stop right after the headers
            assert response.startswith(b"HTTP/1.1 101"), response
            return sock

        return await asyncio.to_thread(handshake)

    async def online(self) -> "Socket":
        """Connect and consume the hello frame."""
        sock = await self.connect()
        hello = await sock.recv()
        assert hello["type"] == "hello", hello
        return sock


@dataclass
class Socket:
    ws: ClientConnection

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        frame: dict[str, Any] = json.loads(raw)
        return frame

    async def recv_type(self, type_: str, timeout: float = 5.0) -> dict[str, Any]:
        """The next frame of `type_`, skipping others."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            frame = await self.recv(max(remaining, 0.01))
            if frame["type"] == type_:
                return frame

    async def none_of_type(self, type_: str, seconds: float) -> None:
        """No frame of `type_` arrives within `seconds` (other frames are ignored)."""
        deadline = asyncio.get_running_loop().time() + seconds
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            try:
                frame = await self.recv(remaining)
            except TimeoutError:
                return
            if frame["type"] == type_:
                raise AssertionError(f"expected no {type_}, got {frame}")

    async def nothing_within(self, seconds: float) -> None:
        try:
            frame = await self.recv(seconds)
        except TimeoutError:
            return
        raise AssertionError(f"expected no frame, got {frame}")

    async def send(self, frame: dict[str, Any] | str) -> None:
        await self.ws.send(frame if isinstance(frame, str) else json.dumps(frame))

    async def close_code(self, timeout: float = 5.0) -> int | None:
        """Read until the server closes; return its close code."""
        from websockets.exceptions import ConnectionClosed

        try:
            async with asyncio.timeout(timeout):
                while True:
                    await self.ws.recv()
        except ConnectionClosed as closed:
            return closed.rcvd.code if closed.rcvd else None

    async def close(self) -> None:
        await self.ws.close()


@pytest.fixture
def ws_settings_overrides() -> dict[str, object]:
    """Override in a test module to change Settings for the live server."""
    return {}


@pytest.fixture
async def live_server(
    make_settings: SettingsFactory,
    test_database: DatabaseUnderTest,
    ws_settings_overrides: dict[str, object],
) -> AsyncIterator[LiveServer]:
    settings = make_settings(
        database_url=test_database.app_url,
        rate_limit_enabled=False,
        allowed_origins=ORIGIN,
        **{**FAST_ARGON2, **ws_settings_overrides},
    )
    app = create_app(settings)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, ws="websockets-sansio")
    )
    serving = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started, "uvicorn did not start"
    # The outbox listener connects in the background; wait until events can flow.
    for _ in range(200):
        if app.state.outbox_listener.connected:
            break
        await asyncio.sleep(0.02)

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        try:
            yield LiveServer(app=app, http=http, ws_base=f"ws://127.0.0.1:{port}")
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=15)
            truncate_all_tables(test_database)


GroupFactory = Callable[..., Any]
