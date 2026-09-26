"""Binding sockets to login sessions (docs/design/06 §11.6, M09).

Two mechanisms close a revoked session's sockets:
1. Immediate: `session.revoked` outbox events (logout, logout-all, password change,
   reuse detection, account deletion) are consumed by the listener hook below, which
   closes the matching sockets with 4001 and does not forward the event.
2. Safety net: every 5 minutes, one batched query re-validates every connected session
   (a live refresh token in its family, an active user); failures are closed with 4001.
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.realtime.connection_manager import ConnectionManager
from app.realtime.envelope import WSEvent

logger = logging.getLogger(__name__)

REVALIDATE_EVERY_SECONDS = 300.0

_LIVE_SESSIONS = text(
    """
    SELECT DISTINCT r.family_id
    FROM refresh_tokens AS r
    JOIN users AS u ON u.id = r.user_id
    WHERE r.family_id = ANY(:families)
      AND r.revoked_at IS NULL
      AND r.expires_at > now()
      AND u.deleted_at IS NULL
      AND u.status = 'active'
    """
)


def session_revoked_hook(manager: ConnectionManager):  # type: ignore[no-untyped-def]
    """Outbox listener hook: act on session.revoked and consume it (return True)."""

    def hook(event: WSEvent) -> bool:
        if event.type != "session.revoked":
            return False
        user_id = UUID(str(event.payload["user_id"]))
        families = event.payload.get("family_ids")
        target = None if families == "*" else {UUID(str(f)) for f in families or []}
        closed = manager.close_sessions(user_id, target)
        logger.info(
            "Session sockets closed",
            extra={"event": "ws.session_revoked", "user_id": str(user_id), "connections": closed},
        )
        return True

    return hook


async def revalidate_sessions(engine: AsyncEngine, manager: ConnectionManager) -> int:
    """Close sockets whose session is no longer valid. Returns how many were closed."""
    sessions = manager.connected_sessions()
    if not sessions:
        return 0
    async with engine.connect() as conn:
        rows = await conn.execute(_LIVE_SESSIONS, {"families": list(sessions)})
        live: set[UUID] = set(rows.scalars())
    closed = 0
    for family_id, user_id in sessions.items():
        if family_id not in live:
            closed += manager.close_sessions(user_id, {family_id})
    if closed:
        logger.info(
            "Stale sessions closed", extra={"event": "ws.sessions_revalidated", "closed": closed}
        )
    return closed


async def revalidation_loop(engine: AsyncEngine, manager: ConnectionManager) -> None:
    while True:
        await asyncio.sleep(REVALIDATE_EVERY_SECONDS)
        await revalidate_sessions(engine, manager)
