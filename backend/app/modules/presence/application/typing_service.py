"""Typing indicators (docs/design/08 §14.8, M08).

Ephemeral and never persisted: membership is checked through a TTL cache, starts are
throttled to one broadcast per (user, conversation) per 2 s, and `typing.updated` goes
to the *other* members' connections only. Receivers expire the indicator after 6 s on
their own, so a crashed sender tab can't leave it stuck.
"""

import time
from collections.abc import Callable
from uuid import UUID

from app.modules.presence.application.ports import Broadcaster, MembershipLookup
from app.platform.errors import AuthorizationError
from app.realtime.envelope import WSEvent

THROTTLE_SECONDS = 2.0
EXPIRES_IN_SECONDS = 6


class NotAMember(AuthorizationError):
    default_code = "not_a_member"
    default_message = "You are not a member of this conversation."


class TypingService:
    def __init__(
        self,
        members: MembershipLookup,
        broadcaster: Broadcaster,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._members = members
        self._broadcaster = broadcaster
        self._monotonic = monotonic
        self._last_start: dict[tuple[UUID, UUID], float] = {}

    async def update(self, *, user_id: UUID, conversation_id: UUID, is_typing: bool) -> bool:
        """Returns whether a broadcast went out. Raises NotAMember."""
        members = await self._members.members_of(conversation_id)
        if user_id not in members:
            raise NotAMember()

        key = (user_id, conversation_id)
        now = self._monotonic()
        if is_typing:
            last = self._last_start.get(key)
            if last is not None and now - last < THROTTLE_SECONDS:
                return False
            self._last_start[key] = now
            self._prune(now)
        else:
            self._last_start.pop(key, None)  # the next start broadcasts at once

        frame = WSEvent(
            type="typing.updated",
            conversation_id=conversation_id,
            payload={
                "conversation_id": str(conversation_id),
                "user_id": str(user_id),
                "is_typing": is_typing,
                "expires_in_s": EXPIRES_IN_SECONDS,
            },
        ).to_frame()
        self._broadcaster.deliver_frame(frame, members - {user_id})
        return True

    def _prune(self, now: float) -> None:
        if len(self._last_start) < 10_000:
            return
        for key, at in list(self._last_start.items()):
            if now - at >= THROTTLE_SECONDS:
                del self._last_start[key]
