"""Scheduled-message use cases on the API side (docs/design/07 §Scheduled messages, M11).

Edit, cancel and retry are each one conditional UPDATE, so a race with the worker is
settled by the database, never by read-then-write in Python (09 §16.4). Every change
publishes `scheduled_message.updated` to the sender, which keeps the scheduled list in
sync across their tabs.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from app.modules.conversations.domain.errors import ConversationNotFound
from app.modules.messaging.domain.errors import ReplyNotInConversation
from app.modules.messaging.domain.message import normalize_body
from app.modules.scheduling.application.payloads import scheduled_payload
from app.modules.scheduling.application.ports import (
    SchedulingUnitOfWork,
    SchedulingUnitOfWorkFactory,
)
from app.modules.scheduling.domain.errors import (
    NotFailed,
    NotPending,
    ScheduledMessageNotFound,
    TooManyPending,
)
from app.modules.scheduling.domain.scheduled_message import (
    ScheduledMessage,
    ScheduledMessageView,
    ScheduledPage,
    ScheduledStatus,
    normalize_scheduled_at,
    validate_timezone,
)
from app.platform.clock import Clock
from app.platform.pagination import InvalidCursor, decode_cursor, encode_cursor

logger = logging.getLogger(__name__)


class _Unset:
    """Marks a PATCH field that wasn't sent (as opposed to one sent as null)."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    min_lead: timedelta = timedelta(seconds=30)
    max_horizon: timedelta = timedelta(days=365)
    max_pending_per_user: int = 100


class SchedulingService:
    def __init__(
        self,
        uow_factory: SchedulingUnitOfWorkFactory,
        clock: Clock,
        policy: SchedulePolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._policy = policy or SchedulePolicy()

    def _instant(self, scheduled_at: datetime) -> datetime:
        return normalize_scheduled_at(
            scheduled_at,
            now=self._clock.now(),
            min_lead=self._policy.min_lead,
            max_horizon=self._policy.max_horizon,
        )

    # --- helpers ---------------------------------------------------------------------

    @staticmethod
    async def _view(uow: SchedulingUnitOfWork, row: ScheduledMessage) -> ScheduledMessageView:
        sent = await uow.scheduled.sent_message_ids([row.id])
        return ScheduledMessageView(row, sent.get(row.id))

    @staticmethod
    async def _require_member(
        uow: SchedulingUnitOfWork, conversation_id: UUID, user_id: UUID
    ) -> None:
        if await uow.conversations.get_active_membership(conversation_id, user_id) is None:
            raise ConversationNotFound()

    @staticmethod
    async def _check_reply(
        uow: SchedulingUnitOfWork, reply_to_id: UUID | None, conversation_id: UUID
    ) -> None:
        if reply_to_id is None:
            return
        target = await uow.messages.get(reply_to_id)
        if target is None or target.conversation_id != conversation_id:
            raise ReplyNotInConversation()

    @staticmethod
    async def _own(
        uow: SchedulingUnitOfWork, scheduled_id: UUID, caller_id: UUID
    ) -> ScheduledMessage:
        row = await uow.scheduled.get(scheduled_id)
        if row is None or row.sender_id != caller_id:
            raise ScheduledMessageNotFound()
        return row

    async def _changed(
        self, uow: SchedulingUnitOfWork, row: ScheduledMessage, action: str
    ) -> ScheduledMessageView:
        """Publish and audit a change, inside its transaction."""
        view = await self._view(uow, row)
        await uow.events.publish(
            "scheduled_message.updated",
            recipient_user_ids=[row.sender_id],
            payload=scheduled_payload(view),
        )
        await uow.audit.record(
            f"scheduled_message.{action}",
            actor_user_id=row.sender_id,
            target_type="scheduled_message",
            target_id=row.id,
            metadata={"conversation_id": str(row.conversation_id)},
        )
        logger.info(
            "Scheduled message %s",
            action,
            extra={
                "event": f"scheduled_message.{action}",
                "scheduled_message_id": str(row.id),
                "conversation_id": str(row.conversation_id),
                "scheduled_at": row.scheduled_at_utc.isoformat(),
                "body_length": len(row.body),  # never the body itself
            },
        )
        return view

    # --- use cases -------------------------------------------------------------------

    async def create(
        self,
        *,
        caller_id: UUID,
        conversation_id: UUID,
        body: str,
        reply_to_id: UUID | None,
        scheduled_at: datetime,
        timezone: str,
        client_message_id: UUID,
    ) -> tuple[ScheduledMessageView, bool]:
        """Returns (view, created). A repeated client_message_id returns the original."""
        text = normalize_body(body)
        instant = self._instant(scheduled_at)
        zone = validate_timezone(timezone)
        async with self._uow_factory() as uow:
            await self._require_member(uow, conversation_id, caller_id)
            existing = await uow.scheduled.get_by_client_id(client_message_id)
            if existing is not None:
                return await self._replay(uow, existing, caller_id), False
            await self._check_reply(uow, reply_to_id, conversation_id)
            if await uow.scheduled.count_pending(caller_id) >= self._policy.max_pending_per_user:
                raise TooManyPending()
            row = await uow.scheduled.insert_on_conflict_client_id(
                conversation_id=conversation_id,
                sender_id=caller_id,
                body=text,
                reply_to_id=reply_to_id,
                client_message_id=client_message_id,
                scheduled_at_utc=instant,
                sender_timezone=zone,
            )
            if row is None:  # a concurrent double submit won the insert
                winner = await uow.scheduled.get_by_client_id(client_message_id)
                assert winner is not None  # noqa: S101 - the conflict proves it exists
                return await self._replay(uow, winner, caller_id), False
            return await self._changed(uow, row, "created"), True

    async def _replay(
        self, uow: SchedulingUnitOfWork, row: ScheduledMessage, caller_id: UUID
    ) -> ScheduledMessageView:
        # Another user's client_message_id: don't reveal that it exists.
        if row.sender_id != caller_id:
            raise ScheduledMessageNotFound()
        return await self._view(uow, row)

    async def get(self, *, caller_id: UUID, scheduled_id: UUID) -> ScheduledMessageView:
        async with self._uow_factory() as uow:
            return await self._view(uow, await self._own(uow, scheduled_id, caller_id))

    async def list_for_user(
        self,
        *,
        caller_id: UUID,
        status: ScheduledStatus | None,
        conversation_id: UUID | None,
        cursor: str | None,
        limit: int,
    ) -> ScheduledPage:
        after: tuple[datetime, UUID] | None = None
        if cursor is not None:
            at, sid = decode_cursor(cursor, length=2)
            try:
                after = (datetime.fromisoformat(at), UUID(sid))
            except (TypeError, ValueError):
                raise InvalidCursor() from None
        async with self._uow_factory() as uow:
            rows = await uow.scheduled.list_for_sender(
                caller_id,
                status=status,
                conversation_id=conversation_id,
                after=after,
                limit=limit + 1,
            )
            page = rows[:limit]
            sent = await uow.scheduled.sent_message_ids([r.id for r in page])
        next_cursor = (
            encode_cursor([page[-1].scheduled_at_utc.isoformat(), str(page[-1].id)])
            if len(rows) > limit
            else None
        )
        return ScheduledPage(
            items=[ScheduledMessageView(r, sent.get(r.id)) for r in page], next_cursor=next_cursor
        )

    async def update(
        self,
        *,
        caller_id: UUID,
        scheduled_id: UUID,
        body: str | None = None,
        scheduled_at: datetime | None = None,
        timezone: str | None = None,
        reply_to_id: UUID | _Unset | None = UNSET,
    ) -> ScheduledMessageView:
        changes: dict[str, Any] = {}
        if body is not None:
            changes["body"] = normalize_body(body)
        if scheduled_at is not None:
            instant = self._instant(scheduled_at)
            changes["scheduled_at_utc"] = instant
            changes["next_attempt_at"] = instant
        if timezone is not None:
            changes["sender_timezone"] = validate_timezone(timezone)
        async with self._uow_factory() as uow:
            current = await self._own(uow, scheduled_id, caller_id)
            if not isinstance(reply_to_id, _Unset):
                await self._check_reply(uow, reply_to_id, current.conversation_id)
                changes["reply_to_id"] = reply_to_id
            if not changes:
                if not current.can_edit:
                    raise NotPending()
                return await self._view(uow, current)
            row = await uow.scheduled.update_pending(scheduled_id, caller_id, changes)
            if row is None:
                raise NotPending()  # sent, cancelled or failed, possibly just now
            return await self._changed(uow, row, "updated")

    async def cancel(self, *, caller_id: UUID, scheduled_id: UUID) -> ScheduledMessageView:
        """Idempotent: cancelling a cancelled message returns it unchanged."""
        async with self._uow_factory() as uow:
            row = await uow.scheduled.cancel(scheduled_id, caller_id)
            if row is not None:
                return await self._changed(uow, row, "cancelled")
            current = await self._own(uow, scheduled_id, caller_id)
            if current.status is ScheduledStatus.CANCELLED:
                return await self._view(uow, current)
            raise NotPending()

    async def retry(
        self, *, caller_id: UUID, scheduled_id: UUID, scheduled_at: datetime | None = None
    ) -> ScheduledMessageView:
        """failed -> pending. Without a time, it goes out as soon as allowed."""
        instant = (
            self._instant(scheduled_at)
            if scheduled_at is not None
            else self._clock.now() + self._policy.min_lead
        )
        async with self._uow_factory() as uow:
            row = await uow.scheduled.reset_failed(
                scheduled_id, caller_id, scheduled_at_utc=instant
            )
            if row is not None:
                return await self._changed(uow, row, "retried")
            await self._own(uow, scheduled_id, caller_id)  # 404 if missing or not theirs
            raise NotFailed()
