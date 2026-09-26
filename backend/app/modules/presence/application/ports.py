"""Ports for presence and typing: storage, delivery, membership, timers."""

from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Protocol
from uuid import UUID

from app.modules.presence.domain.presence import Presence, PresenceStatus


class PresenceStore(Protocol):
    async def upsert_status(self, user_id: UUID, status: PresenceStatus) -> Presence: ...
    async def get_many(self, user_ids: Sequence[UUID]) -> dict[UUID, Presence]: ...
    async def co_member_ids(self, user_id: UUID) -> set[UUID]: ...


class Broadcaster(Protocol):
    """Ephemeral delivery straight to connections: no outbox, never replayed."""

    def deliver_frame(self, frame: str, user_ids: Iterable[UUID]) -> int: ...
    def is_online(self, user_id: UUID) -> bool: ...


class MembershipLookup(Protocol):
    async def members_of(self, conversation_id: UUID) -> frozenset[UUID]: ...


class Cancellable(Protocol):
    def cancel(self) -> object: ...


# schedule(delay_seconds, callback) -> handle. Injected so tests can run timers by hand.
Scheduler = Callable[[float, Callable[[], Awaitable[None]]], Cancellable]
