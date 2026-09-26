"""The outbox listener: cross-process delivery (docs/design/08 §14.4).

A dedicated asyncpg connection (outside the SQLAlchemy pool: LISTEN needs one long-
lived session) runs `LISTEN events`. Notified ids are micro-batched (20 ms or 100 ids),
the rows are re-read from event_outbox, and each event goes to the ConnectionManager.

REST requests and the scheduler worker both write to the outbox, so events from either
process take this same path.

If the connection drops, `run` raises and the supervisor restarts it with backoff.
NOTIFYs sent while disconnected are lost (Postgres doesn't queue them), so on every
reconnect after the first the `on_reconnect` hook runs; M09 uses it to tell every
client to sync.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from uuid import UUID

import asyncpg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.platform.logging import bind_log_context
from app.realtime.connection_manager import ConnectionManager
from app.realtime.envelope import WSEvent
from app.realtime.publisher import EventOutboxModel

logger = logging.getLogger(__name__)

CHANNEL = "events"
BATCH_WINDOW_SECONDS = 0.02
BATCH_MAX_IDS = 100
KEEPALIVE_SECONDS = 15.0

EventHook = Callable[[WSEvent], None]


def libpq_dsn(sqlalchemy_url: str) -> str:
    """postgresql+asyncpg://… → postgresql://… for asyncpg.connect."""
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class ListenerUnavailable(Exception):
    pass


class OutboxListener:
    def __init__(
        self,
        *,
        dsn: str,
        engine: AsyncEngine,
        manager: ConnectionManager,
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        self._dsn = dsn
        self._engine = engine
        self._manager = manager
        self._on_reconnect = on_reconnect
        self._pending: list[int] = []
        self._wake = asyncio.Event()
        self._has_connected_before = False
        self.connected = False
        # Internal handling of special events before fan-out (e.g. M08's membership
        # cache, M09's session.revoked).
        self.hooks: list[EventHook] = []

    async def check_ready(self) -> None:
        """Readiness check: the listener must be connected (docs/design/07 §Health)."""
        if not self.connected:
            raise ListenerUnavailable("outbox listener is not connected")

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            self._pending.append(int(payload))
        except ValueError:
            logger.warning("Ignoring malformed NOTIFY", extra={"event": "ws.listener_bad_notify"})
            return
        self._wake.set()

    async def run(self) -> None:
        """Listen until the connection is lost, then raise so the supervisor restarts us."""
        connection: asyncpg.Connection = await asyncpg.connect(self._dsn)
        lost = asyncio.Event()
        connection.add_termination_listener(lambda _conn: lost.set())
        try:
            await connection.add_listener(CHANNEL, self._on_notify)
            self.connected = True
            if self._has_connected_before:
                logger.warning(
                    "Outbox listener reconnected", extra={"event": "ws.listener_reconnected"}
                )
                if self._on_reconnect is not None:
                    self._on_reconnect()
            else:
                logger.info("Outbox listener connected", extra={"event": "ws.listener_connected"})
            self._has_connected_before = True
            await self._loop(connection, lost)
        finally:
            self.connected = False
            if not connection.is_closed():
                connection.terminate()
        raise ListenerUnavailable("outbox listener connection lost")

    async def _loop(self, connection: asyncpg.Connection, lost: asyncio.Event) -> None:
        while not lost.is_set():
            woke = asyncio.ensure_future(self._wake.wait())
            died = asyncio.ensure_future(lost.wait())
            done, pending = await asyncio.wait(
                {woke, died}, timeout=KEEPALIVE_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
            for future in pending:
                future.cancel()
            if lost.is_set():
                return
            if not done:
                # Idle: a cheap query detects a half-open connection.
                await connection.execute("SELECT 1")
                continue
            # Micro-batch: give concurrent commits a moment to arrive together.
            if len(self._pending) < BATCH_MAX_IDS:
                await asyncio.sleep(BATCH_WINDOW_SECONDS)
            self._wake.clear()
            ids, self._pending = self._pending[:], []
            for start in range(0, len(ids), BATCH_MAX_IDS):
                await self.dispatch(ids[start : start + BATCH_MAX_IDS])

    async def dispatch(self, ids: Sequence[int]) -> None:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(EventOutboxModel)
                    .where(EventOutboxModel.id.in_(ids))
                    .order_by(EventOutboxModel.id)
                )
            ).all()
        for row in rows:
            event = WSEvent(
                id=row.id,
                type=row.type,
                conversation_id=row.conversation_id,
                occurred_at=row.created_at,
                correlation_id=row.correlation_id,
                payload=row.payload,
                recipient_user_ids=tuple(UUID(str(u)) for u in row.recipient_user_ids),
            )
            # Fan-out logs link back to the request (or worker job) that caused them.
            with bind_log_context(request_id=event.correlation_id):
                for hook in self.hooks:
                    hook(event)
                delivered = self._manager.deliver(event)
                logger.debug(
                    "Event delivered",
                    extra={
                        "event": "ws.event_delivered",
                        "event_id": event.id,
                        "type": event.type,
                        "connections": delivered,
                    },
                )
