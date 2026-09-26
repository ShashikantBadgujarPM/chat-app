"""Presence (docs/design/08 §14.7-14.8, M08).

Online means "has at least one connection". Transitions happen on 0<->1 connections:
- 0 -> 1: persist online and tell co-members, unless the user is coming back within
  the grace period, in which case nothing changed for anyone else (R-8).
- 1 -> 0: start a grace timer. Only if no connection returns before it fires is the
  user persisted offline (with last_seen_at) and co-members told.

Transitions involve a DB write (an await), so they are serialized per user with an
asyncio.Lock; otherwise a fast close -> open could persist `offline` after `online`.
Presence events are ephemeral (id null): sent directly, never through the outbox.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.modules.presence.application.ports import (
    Broadcaster,
    Cancellable,
    PresenceStore,
    Scheduler,
)
from app.modules.presence.domain.presence import Presence, PresenceStatus
from app.realtime.envelope import WSEvent

logger = logging.getLogger(__name__)

CO_MEMBER_TTL_SECONDS = 60.0


def presence_payload(presence: Presence) -> dict[str, Any]:
    return {
        "user_id": str(presence.user_id),
        "status": presence.status.value,
        "last_seen_at": (
            presence.last_seen_at.isoformat().replace("+00:00", "Z")
            if presence.last_seen_at
            else None
        ),
    }


class PresenceService:
    def __init__(
        self,
        store: PresenceStore,
        broadcaster: Broadcaster,
        *,
        grace_seconds: float,
        schedule: Scheduler,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._broadcaster = broadcaster
        self._grace_seconds = grace_seconds
        self._schedule = schedule
        self._monotonic = monotonic
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._grace_timers: dict[UUID, Cancellable] = {}
        self._co_members: dict[UUID, tuple[float, set[UUID]]] = {}

    def _lock(self, user_id: UUID) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def connection_opened(self, user_id: UUID) -> None:
        async with self._lock(user_id):
            timer = self._grace_timers.get(user_id)
            if timer is not None:
                if self._broadcaster.is_online(user_id):
                    # Back within the grace period: nobody saw them leave (R-8).
                    timer.cancel()
                    del self._grace_timers[user_id]
                return
            if not self._broadcaster.is_online(user_id):
                return  # already gone again; the close path handles it
            presence = await self._store.upsert_status(user_id, PresenceStatus.ONLINE)
            await self._announce(presence)

    def connection_closed(self, user_id: UUID) -> None:
        """Last connection closed: go offline only if nobody returns in time."""
        previous = self._grace_timers.pop(user_id, None)
        if previous is not None:
            previous.cancel()
        self._grace_timers[user_id] = self._schedule(
            self._grace_seconds, lambda: self._grace_expired(user_id)
        )

    async def _grace_expired(self, user_id: UUID) -> None:
        async with self._lock(user_id):
            self._grace_timers.pop(user_id, None)
            if self._broadcaster.is_online(user_id):
                return
            presence = await self._store.upsert_status(user_id, PresenceStatus.OFFLINE)
            await self._announce(presence)

    async def _announce(self, presence: Presence) -> None:
        audience = await self._audience(presence.user_id)
        frame = WSEvent(type="presence.updated", payload=presence_payload(presence)).to_frame()
        delivered = self._broadcaster.deliver_frame(frame, audience)
        logger.info(
            "Presence changed",
            extra={
                "event": "presence.updated",
                "user_id": str(presence.user_id),
                "status": presence.status.value,
                "connections": delivered,
            },
        )

    async def _audience(self, user_id: UUID) -> set[UUID]:
        cached = self._co_members.get(user_id)
        now = self._monotonic()
        if cached is not None and now - cached[0] < CO_MEMBER_TTL_SECONDS:
            return cached[1]
        members = await self._store.co_member_ids(user_id)
        self._co_members[user_id] = (now, members)
        return members

    def forget_co_members(self, user_ids: set[UUID] | None = None) -> None:
        """Membership changed: recompute audiences on the next transition."""
        if user_ids is None:
            self._co_members.clear()
        else:
            for user_id in user_ids:
                self._co_members.pop(user_id, None)

    async def get_presence(self, user_ids: list[UUID]) -> dict[UUID, Presence]:
        return await self._store.get_many(user_ids)
