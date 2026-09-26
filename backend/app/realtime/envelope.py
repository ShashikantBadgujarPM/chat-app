"""The WebSocket event envelope (docs/design/08 §15.1)."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

ENVELOPE_VERSION = 1


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class WSEvent:
    """A server -> client frame. Durable events carry their outbox id; ephemeral and
    control frames (hello, pong, typing, presence, sync control) have id=None."""

    type: str
    payload: dict[str, Any]
    id: int | None = None
    conversation_id: UUID | None = None
    occurred_at: datetime | None = None
    correlation_id: str | None = None
    recipient_user_ids: tuple[UUID, ...] = ()

    def to_frame(self) -> str:
        return json.dumps(
            {
                "v": ENVELOPE_VERSION,
                "id": self.id,
                "type": self.type,
                "conversation_id": str(self.conversation_id) if self.conversation_id else None,
                "occurred_at": _iso(self.occurred_at or datetime.now(UTC)),
                "correlation_id": self.correlation_id,
                "payload": self.payload,
            },
            separators=(",", ":"),
            default=str,
        )


def control(type_: str, payload: dict[str, Any] | None = None, *, ref: str | None = None) -> str:
    """A control frame (hello, pong, ack, error, ...): no id, no conversation."""
    frame = json.loads(WSEvent(type=type_, payload=payload or {}).to_frame())
    if ref is not None:
        frame["ref"] = ref
    return json.dumps(frame, separators=(",", ":"))
