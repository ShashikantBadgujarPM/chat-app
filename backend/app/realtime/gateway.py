"""The `/ws` endpoint (docs/design/08 §14.2, §14.6, §15.1-15.3).

WS is delivery only (08 §14.1): no client frame mutates durable state. In M06 the only
client frame is `ping`; M08 adds typing and M09 the sync protocol.
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket
from sqlalchemy import func, select

from app.config import Settings
from app.modules.identity.application.ws_ticket_service import WsTicketService
from app.modules.identity.infrastructure.unit_of_work import SqlIdentityUnitOfWork
from app.platform.logging import bind_log_context
from app.realtime.connection_manager import Connection, ConnectionManager
from app.realtime.envelope import control
from app.realtime.publisher import EventOutboxModel

logger = logging.getLogger(__name__)

router = APIRouter()

# Close codes (08 §14.7).
NORMAL = 1000
GOING_AWAY = 1001
INTERNAL_ERROR = 1011
BAD_FRAME = 4000
UNAUTHORIZED = 4001
RATE_LIMITED = 4029

INVALID_FRAMES_PER_MINUTE = 10
FRAMES_PER_SECOND = 20
DROPPED_FRAMES_BEFORE_CLOSE = 200


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _FrameRateLimiter:
    """20 frames/s per connection; extras are dropped (08 §14.3)."""

    def __init__(self) -> None:
        self._tokens = float(FRAMES_PER_SECOND)
        self._updated = time.monotonic()
        self.dropped = 0

    def allow(self) -> bool:
        now = time.monotonic()
        self._tokens = min(
            float(FRAMES_PER_SECOND), self._tokens + (now - self._updated) * FRAMES_PER_SECOND
        )
        self._updated = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        self.dropped += 1
        return False


async def _latest_event_id(websocket: WebSocket) -> int:
    async with websocket.app.state.engine.connect() as conn:
        latest = (await conn.execute(select(func.max(EventOutboxModel.id)))).scalar()
    return int(latest or 0)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, ticket: str | None = None) -> None:
    app = websocket.app
    settings: Settings = app.state.settings

    # Browsers always send Origin on a WS upgrade, and CORS doesn't protect WebSockets.
    # A non-browser client may omit it; it can't be the victim of cross-site hijacking.
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in settings.allowed_origins:
        logger.warning(
            "WebSocket origin rejected", extra={"event": "ws.origin_rejected", "origin": origin}
        )
        await websocket.close(code=1008)  # before accept: the upgrade gets HTTP 403
        return

    owner = None
    if ticket:
        owner = await WsTicketService(
            lambda: SqlIdentityUnitOfWork(app.state.session_factory)
        ).consume(ticket)
    # Accept, then close with 4001: a browser can't see why an upgrade was refused
    # (only 1006), but it can read our close code and re-authenticate (08 §14.2).
    await websocket.accept()
    if owner is None:
        logger.warning("WebSocket authentication failed", extra={"event": "ws.auth_failed"})
        await websocket.close(code=UNAUTHORIZED, reason="invalid_ticket")
        return

    manager: ConnectionManager = app.state.connections
    connection = manager.new_connection(
        websocket, user_id=owner.user_id, session_family_id=owner.session_family_id
    )
    with bind_log_context(connection_id=connection.id, user_id=str(owner.user_id)):
        manager.register(connection)
        logger.info(
            "WebSocket connected",
            extra={
                "event": "ws.connected",
                "user_agent": websocket.headers.get("user-agent"),
            },
        )
        close_code = NORMAL
        try:
            connection.enqueue(
                control(
                    "hello",
                    {
                        "connection_id": connection.id,
                        "user_id": str(owner.user_id),
                        "server_time": _now_iso(),
                        "heartbeat_interval_s": settings.ws_heartbeat_interval_seconds,
                        "latest_event_id": await _latest_event_id(websocket),
                    },
                )
            )
            close_code = await _receive_loop(connection, settings)
        except Exception:
            logger.exception("WebSocket handler failed", extra={"event": "ws.handler_failed"})
            close_code = INTERNAL_ERROR
            await connection.close(INTERNAL_ERROR, "internal_error")
        finally:
            manager.unregister(connection)
            logger.info(
                "WebSocket disconnected",
                extra={
                    "event": "ws.disconnected",
                    "close_code": connection.close_code or close_code,
                    "duration_s": round(time.monotonic() - connection.connected_at, 3),
                    "frames_in": connection.frames_in,
                    "frames_out": connection.frames_out,
                },
            )


async def _receive_loop(connection: Connection, settings: Settings) -> int:
    """Read client frames until the socket closes. Returns the close code."""
    invalid_at: deque[float] = deque()
    limiter = _FrameRateLimiter()

    def invalid(code: str, message: str, ref: str | None = None) -> int | None:
        connection.enqueue(control("error", {"code": code, "message": message}, ref=ref))
        now = time.monotonic()
        invalid_at.append(now)
        while invalid_at and now - invalid_at[0] > 60:
            invalid_at.popleft()
        if len(invalid_at) > INVALID_FRAMES_PER_MINUTE:
            return BAD_FRAME
        return None

    while True:
        try:
            async with asyncio.timeout(settings.ws_idle_timeout_seconds):
                message = await connection.websocket.receive()
        except TimeoutError:
            await connection.close(GOING_AWAY, "idle_timeout")
            return GOING_AWAY
        if message["type"] == "websocket.disconnect":
            return int(message.get("code") or NORMAL)
        if connection.closing:
            return connection.close_code or NORMAL

        connection.frames_in += 1
        connection.last_client_frame_at = time.monotonic()
        if not limiter.allow():
            if limiter.dropped > DROPPED_FRAMES_BEFORE_CLOSE:
                await connection.close(RATE_LIMITED, "rate_limited")
                return RATE_LIMITED
            continue

        raw = message.get("text")
        if raw is None:
            verdict = invalid("bad_frame", "Only text frames are accepted.")
        elif len(raw.encode("utf-8")) > settings.ws_max_frame_bytes:
            verdict = invalid("frame_too_large", "The frame is larger than 16 KB.")
        else:
            verdict = _handle_frame(connection, raw, invalid)
        if verdict is not None:
            await connection.close(verdict, "bad_frame")
            return verdict


InvalidFrame = Callable[..., int | None]


def _handle_frame(connection: Connection, raw: str, invalid: InvalidFrame) -> int | None:
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        return invalid("invalid_json", "The frame is not valid JSON.")
    if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
        return invalid("invalid_frame", "A frame must be an object with a string `type`.")
    ref = frame.get("ref") if isinstance(frame.get("ref"), str) else None
    frame_type = frame["type"]

    if frame_type == "ping":
        connection.enqueue(control("pong", {"server_time": _now_iso()}, ref=ref))
        return None
    return invalid("unknown_type", f"Unknown frame type: {frame_type[:64]}", ref=ref)
