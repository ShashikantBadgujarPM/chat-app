"""The EventPublisher port (docs/design/02 §4.2, ADR-004).

Use cases call `publish` inside their transaction for every durable real-time event.
Until the outbox exists (M06) the publisher is a no-op; M06 swaps in the transactional
outbox implementation without changing any caller.
"""

from collections.abc import Iterable, Mapping
from typing import Any, Protocol
from uuid import UUID


class EventPublisher(Protocol):
    async def publish(
        self,
        event_type: str,
        *,
        recipient_user_ids: Iterable[UUID],
        payload: Mapping[str, Any],
        conversation_id: UUID | None = None,
    ) -> None:
        """Record the event in the caller's transaction; delivered only after commit."""
        ...


class NoOpEventPublisher:
    async def publish(
        self,
        event_type: str,
        *,
        recipient_user_ids: Iterable[UUID],
        payload: Mapping[str, Any],
        conversation_id: UUID | None = None,
    ) -> None:
        del event_type, recipient_user_ids, payload, conversation_id
