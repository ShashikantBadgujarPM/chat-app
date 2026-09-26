"""Presence (docs/design/08 §14.7-14.8). Pure domain code."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class PresenceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class Presence:
    user_id: UUID
    status: PresenceStatus
    last_seen_at: datetime | None  # None for a user never seen (no row yet)
