"""Presence lookups for API projections (UserPublic.presence, 07 §Users)."""

from collections.abc import Iterable
from uuid import UUID

from fastapi import Request

from app.modules.identity.api.schemas import PresenceOut
from app.modules.presence.domain.presence import Presence

PresenceMap = dict[UUID, PresenceOut]


def to_presence_out(presence: Presence) -> PresenceOut:
    return PresenceOut(status=presence.status.value, last_seen_at=presence.last_seen_at)


async def presence_for(request: Request, user_ids: Iterable[UUID]) -> PresenceMap:
    ids = list(dict.fromkeys(user_ids))
    found = await request.app.state.presence_store.get_many(ids)
    return {uid: to_presence_out(p) for uid, p in found.items()}
