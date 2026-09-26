"""Active members per conversation, cached for 60 s (docs/design/08 §14.3).

Used for typing authorization on the hot WS path, where a DB query per keystroke would
be wasteful. Membership events (`conversation.member_added/removed`) invalidate the
entry via the outbox listener hook, so the TTL is only a backstop.
"""

import time
from collections.abc import Awaitable, Callable
from uuid import UUID

from app.realtime.envelope import WSEvent

TTL_SECONDS = 60.0
INVALIDATING_EVENTS = frozenset(
    {"conversation.member_added", "conversation.member_removed", "conversation.created"}
)

MemberLoader = Callable[[UUID], Awaitable[frozenset[UUID]]]


class MembershipCache:
    def __init__(
        self, loader: MemberLoader, *, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._loader = loader
        self._monotonic = monotonic
        self._entries: dict[UUID, tuple[float, frozenset[UUID]]] = {}

    async def members_of(self, conversation_id: UUID) -> frozenset[UUID]:
        now = self._monotonic()
        entry = self._entries.get(conversation_id)
        if entry is not None and now - entry[0] < TTL_SECONDS:
            return entry[1]
        members = await self._loader(conversation_id)
        self._entries[conversation_id] = (now, members)
        return members

    def invalidate(self, conversation_id: UUID) -> None:
        self._entries.pop(conversation_id, None)

    def on_event(self, event: WSEvent) -> None:
        """Outbox listener hook."""
        if event.type in INVALIDATING_EVENTS and event.conversation_id is not None:
            self.invalidate(event.conversation_id)
