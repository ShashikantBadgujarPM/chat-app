"""Scheduled-message delivery, on the worker side (docs/design/09 §17.2-17.4, M11).

One transaction per batch: claim due rows with FOR UPDATE SKIP LOCKED, then deliver
each inside its own savepoint, so one bad item can't undo the others. Delivery calls
MessagingService.create_message, the same write path as the REST endpoint, and marks
the row sent in the same transaction: the message and the "sent" status commit
together or not at all.
"""

import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from app.modules.messaging.application.messaging_service import MessagingService
from app.modules.scheduling.application.payloads import scheduled_payload
from app.modules.scheduling.application.ports import (
    SchedulingUnitOfWork,
    SchedulingUnitOfWorkFactory,
)
from app.modules.scheduling.domain.scheduled_message import (
    Permanent,
    PermanentDeliveryError,
    ScheduledMessage,
    ScheduledMessageView,
    backoff,
    classify_failure,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"

# Test hook: runs after the batch is claimed (row locks held), before delivery.
OnClaimed = Callable[[Sequence[ScheduledMessage]], Awaitable[None]]


class Outcome(StrEnum):
    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    max_lateness: timedelta = timedelta(hours=24)
    max_attempts: int = 5


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    outcome: Outcome
    scheduled: ScheduledMessage  # the row after this attempt
    message_id: UUID | None = None
    lateness_ms: int = 0
    duration_ms: int = 0
    error: str | None = None


class DeliveryService:
    def __init__(
        self,
        uow_factory: SchedulingUnitOfWorkFactory,
        messaging: MessagingService,
        policy: DeliveryPolicy | None = None,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._messaging = messaging
        self._policy = policy or DeliveryPolicy()
        self._rng = rng

    async def deliver_due(
        self, batch_size: int, *, on_claimed: OnClaimed | None = None
    ) -> list[DeliveryResult]:
        """Claim and process one batch. Results are logged only after the commit, so
        the logs never report a send that was rolled back."""
        results: list[DeliveryResult] = []
        async with self._uow_factory() as uow:
            db_now, rows = await uow.scheduled.claim_due(batch_size)
            if not rows:
                return []
            assert db_now is not None  # noqa: S101 - claim_due returns it with rows
            logger.info(
                "Scheduled messages claimed",
                extra={"event": "scheduler.batch_claimed", "claimed": len(rows)},
            )
            if on_claimed is not None:
                await on_claimed(rows)
            for row in rows:
                results.append(await self.process(uow, row, db_now=db_now))
        for result in results:
            _log_result(result)
        return results

    async def process(
        self, uow: SchedulingUnitOfWork, row: ScheduledMessage, *, db_now: datetime
    ) -> DeliveryResult:
        """Deliver one claimed row in a savepoint, and record the outcome on the row.

        After a rollback to the savepoint the outer transaction still holds the row
        lock, so recording the failure is safe."""
        started = time.perf_counter()
        try:
            async with uow.savepoint():
                return await self._deliver(uow, row, db_now=db_now, started=started)
        except Exception as exc:
            failure = classify_failure(exc)
            if isinstance(failure, Permanent):
                return await self._fail(uow, row, failure.reason, started=started)
            if row.attempts + 1 >= self._policy.max_attempts:
                logger.error(
                    "Scheduled delivery kept failing",
                    exc_info=True,
                    extra={
                        "event": "scheduler.delivery_error",
                        "scheduled_message_id": str(row.id),
                    },
                )
                return await self._fail(uow, row, MAX_ATTEMPTS_EXCEEDED, started=started)
            if not _is_database_error(exc):
                # Unexpected: worth a stack trace, not just the retry line.
                logger.error(
                    "Unexpected error delivering a scheduled message",
                    exc_info=True,
                    extra={
                        "event": "scheduler.delivery_error",
                        "scheduled_message_id": str(row.id),
                    },
                )
            updated = await uow.scheduled.mark_retry(
                row.id,
                next_attempt_at=db_now + backoff(row.attempts + 1, rng=self._rng),
                error=failure.error_class,
            )
            return DeliveryResult(
                Outcome.RETRY, updated, error=failure.error_class, duration_ms=_ms_since(started)
            )

    async def _deliver(
        self,
        uow: SchedulingUnitOfWork,
        row: ScheduledMessage,
        *,
        db_now: datetime,
        started: float,
    ) -> DeliveryResult:
        # 1. Re-validate against the current state, not the state at scheduling time.
        lateness = db_now - row.scheduled_at_utc
        if lateness > self._policy.max_lateness:
            raise PermanentDeliveryError("expired")
        if not await uow.users.active_user_ids([row.sender_id]):
            raise PermanentDeliveryError("sender_inactive")

        # 2. The shared write path: seq, the message, message.created to all members.
        #    It raises ConversationNotFound if the sender is no longer a member.
        #    UNIQUE(scheduled_message_id) plus the carried-over client_message_id make
        #    a second message impossible, even under a bug.
        view, created = await self._messaging.create_message(
            uow,
            sender_id=row.sender_id,
            conversation_id=row.conversation_id,
            body=row.body,
            reply_to_id=row.reply_to_id,
            client_message_id=row.client_message_id,
            scheduled_message_id=row.id,
        )
        if not created:
            # Only possible if a message with this client_message_id already exists:
            # the send already happened, so finishing the bookkeeping is the recovery.
            logger.warning(
                "Scheduled message was already materialized",
                extra={
                    "event": "scheduler.already_sent",
                    "scheduled_message_id": str(row.id),
                    "message_id": str(view.message.id),
                },
            )

        # 3-5. Mark sent, tell the sender, audit. All in this same transaction.
        sent = await uow.scheduled.mark_sent(row.id)
        payload = scheduled_payload(ScheduledMessageView(sent, view.message.id))
        await uow.events.publish(
            "scheduled_message.sent",
            recipient_user_ids=[row.sender_id],
            payload={"scheduled_message": payload, "message_id": str(view.message.id)},
        )
        await uow.audit.record(
            "scheduled_message.sent",
            actor_user_id=row.sender_id,
            target_type="scheduled_message",
            target_id=row.id,
            metadata={"message_id": str(view.message.id), "attempt": sent.attempts},
        )
        # TODO(M10): mention notifications come from create_message once M10 lands.
        return DeliveryResult(
            Outcome.SENT,
            sent,
            message_id=view.message.id,
            lateness_ms=max(int(lateness.total_seconds() * 1000), 0),
            duration_ms=_ms_since(started),
        )

    async def _fail(
        self, uow: SchedulingUnitOfWork, row: ScheduledMessage, reason: str, *, started: float
    ) -> DeliveryResult:
        failed = await uow.scheduled.mark_failed(row.id, reason=reason, count_attempt=True)
        await uow.events.publish(
            "scheduled_message.failed",
            recipient_user_ids=[row.sender_id],
            payload=scheduled_payload(ScheduledMessageView(failed, None)),
        )
        await uow.audit.record(
            "scheduled_message.failed",
            actor_user_id=row.sender_id,
            target_type="scheduled_message",
            target_id=row.id,
            metadata={"reason": reason, "attempts": failed.attempts},
        )
        # TODO(M10): also create a `scheduled_failed` notification for the sender.
        return DeliveryResult(Outcome.FAILED, failed, error=reason, duration_ms=_ms_since(started))


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _is_database_error(exc: BaseException) -> bool:
    # Checked by name: the application layer doesn't import SQLAlchemy.
    return any(cls.__name__ in {"DBAPIError", "OperationalError"} for cls in type(exc).__mro__)


def _log_result(result: DeliveryResult) -> None:
    row = result.scheduled
    if result.outcome is Outcome.SENT:
        logger.info(
            "Scheduled message sent",
            extra={
                "event": "scheduler.message_sent",
                "scheduled_message_id": str(row.id),
                "message_id": str(result.message_id),
                "conversation_id": str(row.conversation_id),
                "attempt": row.attempts,
                "lateness_ms": result.lateness_ms,
                "duration_ms": result.duration_ms,
            },
        )
    elif result.outcome is Outcome.RETRY:
        logger.warning(
            "Scheduled message will be retried",
            extra={
                "event": "scheduler.message_retry",
                "scheduled_message_id": str(row.id),
                "attempt": row.attempts,
                "error_class": result.error,
                "next_attempt_at": row.next_attempt_at.isoformat(),
            },
        )
    else:
        logger.error(
            "Scheduled message failed",
            extra={
                "event": "scheduler.message_failed",
                "scheduled_message_id": str(row.id),
                "reason": result.error,
                "attempts": row.attempts,
            },
        )
