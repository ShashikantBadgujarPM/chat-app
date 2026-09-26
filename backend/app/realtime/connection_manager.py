"""Connections and fan-out (docs/design/08 §14.3).

Fan-out never awaits a socket: `deliver` puts an already-serialized frame on each
target connection's bounded queue with `put_nowait`, and a per-connection sender task
drains it. A connection whose queue is full is closed with 4008 `slow_consumer` (the
client reconnects and syncs), so one stuck tab can't delay everyone else.

The manager's dicts are mutated only on the event loop, with no `await` between
reading and writing, so asyncio's cooperative scheduling makes each operation atomic
without a lock.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import Final
from uuid import UUID, uuid4

from starlette.websockets import WebSocket, WebSocketState

from app.platform.tasks import TaskSupervisor
from app.realtime.envelope import WSEvent

logger = logging.getLogger(__name__)

SLOW_CONSUMER: Final = 4008
CLOSE_TIMEOUT_SECONDS: Final = 2.0


class _Stop:
    """Queue sentinel: the sender should stop."""


_STOP: Final = _Stop()


class Connection:
    def __init__(
        self,
        websocket: WebSocket,
        *,
        user_id: UUID,
        session_family_id: UUID,
        queue_size: int,
        min_event_id: int = 0,
    ) -> None:
        self.id = uuid4().hex
        self.websocket = websocket
        self.user_id = user_id
        self.session_family_id = session_family_id
        # The outbox id read just before this connection registered (08 §14.4). An
        # event this old already happened before the connection existed: something
        # committed and dispatched between that read and registration is a genuine
        # live event and must still be delivered, so this is a floor, not a cursor.
        self.min_event_id = min_event_id
        self.queue: asyncio.Queue[str | _Stop] = asyncio.Queue(maxsize=queue_size)
        self.connected_at = time.monotonic()
        self.last_client_frame_at = self.connected_at
        self.frames_in = 0
        self.frames_out = 0
        self.closing = False
        # Set synchronously when a close is scheduled, so a burst of overflowing
        # frames schedules (and logs) it once.
        self.close_requested = False
        self.close_code: int | None = None
        self.sender_task: asyncio.Task[None] | None = None

    def enqueue(self, frame: str) -> bool:
        """Queue a frame without waiting. False if the queue is full."""
        if self.closing or self.close_requested:
            return True  # dropping frames for a closing connection is expected
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            return False
        return True

    async def run_sender(self) -> None:
        while True:
            frame = await self.queue.get()
            if isinstance(frame, _Stop):
                return
            await self.websocket.send_text(frame)
            self.frames_out += 1

    async def close(self, code: int, reason: str = "") -> None:
        if self.closing:
            return
        self.closing = True
        self.close_code = code
        if self.sender_task is not None:
            self.sender_task.cancel()
        if self.websocket.application_state != WebSocketState.DISCONNECTED:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    await self.websocket.close(code=code, reason=reason)
            except Exception:
                logger.debug("Close failed", extra={"event": "ws.close_failed", "code": code})

    async def drain_and_close(self, code: int, reason: str, *, drain_seconds: float) -> None:
        """Graceful shutdown: let queued frames go out (up to `drain_seconds`), then close."""
        if self.sender_task is not None and not self.closing:
            try:
                self.queue.put_nowait(_STOP)
                async with asyncio.timeout(drain_seconds):
                    await asyncio.shield(self.sender_task)
            except (asyncio.QueueFull, TimeoutError, asyncio.CancelledError, Exception):
                # Best effort: whatever didn't drain is recovered by the client's sync.
                logger.debug("Drain incomplete", extra={"event": "ws.drain_incomplete"})
        await self.close(code, reason)


class ConnectionManager:
    def __init__(self, *, supervisor: TaskSupervisor, queue_size: int = 256) -> None:
        self._supervisor = supervisor
        self._queue_size = queue_size
        self._by_user: dict[UUID, dict[str, Connection]] = {}
        self._by_session: dict[UUID, set[str]] = {}
        # Called with (user_id, is_first) on register and (user_id, is_last) on
        # unregister, for presence (M08).
        self.on_first_connection: Callable[[UUID], None] | None = None
        self.on_last_disconnection: Callable[[UUID], None] | None = None

    # --- registration ----------------------------------------------------------------

    def new_connection(
        self,
        websocket: WebSocket,
        *,
        user_id: UUID,
        session_family_id: UUID,
        min_event_id: int = 0,
    ) -> Connection:
        return Connection(
            websocket,
            user_id=user_id,
            session_family_id=session_family_id,
            queue_size=self._queue_size,
            min_event_id=min_event_id,
        )

    def register(self, connection: Connection) -> None:
        connections = self._by_user.setdefault(connection.user_id, {})
        first = not connections
        connections[connection.id] = connection
        self._by_session.setdefault(connection.session_family_id, set()).add(connection.id)
        connection.sender_task = self._supervisor.spawn(
            self._send_or_close(connection), name=f"ws-sender-{connection.id}"
        )
        if first and self.on_first_connection is not None:
            self.on_first_connection(connection.user_id)

    def unregister(self, connection: Connection) -> None:
        connections = self._by_user.get(connection.user_id)
        if connections is None or connections.pop(connection.id, None) is None:
            return
        if not connections:
            del self._by_user[connection.user_id]
            if self.on_last_disconnection is not None:
                self.on_last_disconnection(connection.user_id)
        session = self._by_session.get(connection.session_family_id)
        if session is not None:
            session.discard(connection.id)
            if not session:
                del self._by_session[connection.session_family_id]
        if connection.sender_task is not None:
            connection.sender_task.cancel()

    async def _send_or_close(self, connection: Connection) -> None:
        try:
            await connection.run_sender()
        except asyncio.CancelledError:
            raise
        except Exception:  # the peer went away mid-send; the receive loop cleans up
            logger.debug("Sender stopped", extra={"event": "ws.sender_stopped"})

    # --- delivery --------------------------------------------------------------------

    def deliver(self, event: WSEvent) -> int:
        """Queue the event for every local connection of its recipients whose
        watermark it clears (below, see `Connection.min_event_id`).

        Returns the number of connections it was queued for. Recipients without a local
        connection are skipped: they catch up through REST state and sync.
        """
        frame: str | None = None  # serialized lazily: most events reach 0-1 connections
        delivered = 0
        for user_id in event.recipient_user_ids:
            for connection in list(self._by_user.get(user_id, {}).values()):
                if event.id is not None and event.id <= connection.min_event_id:
                    # Committed (and, per this connection, dispatched) before it
                    # registered: already reflected in its initial REST-fetched state.
                    continue
                if frame is None:
                    frame = event.to_frame()
                if connection.enqueue(frame):
                    delivered += 1
                else:
                    self._close_slow_consumer(connection)
        return delivered

    def deliver_frame(self, frame: str, user_ids: Iterable[UUID]) -> int:
        delivered = 0
        for user_id in user_ids:
            for connection in list(self._by_user.get(user_id, {}).values()):
                if connection.enqueue(frame):
                    delivered += 1
                else:
                    self._close_slow_consumer(connection)
        return delivered

    def broadcast(self, frame: str) -> int:
        """Queue a frame for every connection (e.g. `sync.required`, M09)."""
        return self.deliver_frame(frame, list(self._by_user))

    def _close_slow_consumer(self, connection: Connection) -> None:
        if connection.closing or connection.close_requested:
            return
        connection.close_requested = True
        logger.warning(
            "Closing slow consumer",
            extra={
                "event": "ws.slow_consumer_closed",
                "connection_id": connection.id,
                "user_id": str(connection.user_id),
                "queue_size": self._queue_size,
            },
        )
        self._supervisor.spawn(
            connection.close(SLOW_CONSUMER, "slow_consumer"), name=f"ws-close-{connection.id}"
        )

    # --- queries and shutdown --------------------------------------------------------

    def connections_for(self, user_id: UUID) -> list[Connection]:
        return list(self._by_user.get(user_id, {}).values())

    def connections_for_session(self, session_family_id: UUID) -> list[Connection]:
        ids = self._by_session.get(session_family_id, set())
        return [
            connection
            for connections in self._by_user.values()
            for connection_id, connection in connections.items()
            if connection_id in ids
        ]

    def is_online(self, user_id: UUID) -> bool:
        return bool(self._by_user.get(user_id))

    @property
    def connection_count(self) -> int:
        return sum(len(c) for c in self._by_user.values())

    @property
    def user_count(self) -> int:
        return len(self._by_user)

    async def close_all(self, code: int, reason: str, *, drain_timeout: float = 5.0) -> None:
        connections = [c for conns in self._by_user.values() for c in conns.values()]
        await asyncio.gather(
            *(c.drain_and_close(code, reason, drain_seconds=drain_timeout) for c in connections),
            return_exceptions=True,
        )
