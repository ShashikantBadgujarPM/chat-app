"""Composition of presence and typing with the connection manager (M08).

Kept out of `main.py` so the lifespan stays readable: this connects the manager's
0<->1 hooks to PresenceService, gives it a timer backed by supervised tasks, resets
persisted presence at startup, and builds the TypingService.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import FastAPI

from app.config import Settings
from app.modules.presence.application.presence_service import PresenceService
from app.modules.presence.application.typing_service import TypingService
from app.modules.presence.infrastructure.store import SqlPresenceStore
from app.platform.tasks import TaskSupervisor
from app.realtime.connection_manager import ConnectionManager
from app.realtime.envelope import WSEvent
from app.realtime.membership_cache import INVALIDATING_EVENTS
from app.realtime.outbox_listener import OutboxListener

logger = logging.getLogger(__name__)

STARTUP_RESET_RETRY_SECONDS = 2.0


def wire_presence(
    app: FastAPI,
    *,
    settings: Settings,
    tasks: TaskSupervisor,
    connections: ConnectionManager,
    listener: OutboxListener,
) -> None:
    store = SqlPresenceStore(app.state.session_factory)

    def schedule(delay: float, callback: Callable[[], Awaitable[None]]) -> asyncio.Task[None]:
        async def later() -> None:
            await asyncio.sleep(delay)
            await callback()

        return tasks.spawn(later(), name="presence-grace")

    presence = PresenceService(
        store, connections, grace_seconds=settings.presence_grace_seconds, schedule=schedule
    )
    app.state.presence = presence
    app.state.presence_store = store
    app.state.typing = TypingService(app.state.membership_cache, connections)

    def on_first_connection(user_id: UUID) -> None:
        # The hook is synchronous (called from register); the DB write runs as a task.
        tasks.spawn(presence.connection_opened(user_id), name="presence-online")

    connections.on_first_connection = on_first_connection
    connections.on_last_disconnection = presence.connection_closed

    def on_membership_event(event: WSEvent) -> None:
        if event.type in INVALIDATING_EVENTS:
            presence.forget_co_members(set(event.recipient_user_ids))

    listener.hooks.append(on_membership_event)
    tasks.spawn(_reset_presence(store), name="presence-reset")


async def _reset_presence(store: SqlPresenceStore) -> None:
    """Nobody is connected to a process that just started (08 §14.7). Retries until the
    database is reachable."""
    while True:
        try:
            reset = await store.reset_all_offline()
        except Exception:
            logger.warning(
                "Presence reset failed; retrying", extra={"event": "presence.reset_retry"}
            )
            await asyncio.sleep(STARTUP_RESET_RETRY_SECONDS)
            continue
        logger.info("Presence reset", extra={"event": "presence.reset", "users": reset})
        return
