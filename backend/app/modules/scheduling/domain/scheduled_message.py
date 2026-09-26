"""Scheduled messages (docs/design/09 §16-17). Pure domain code: no framework imports."""

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.modules.conversations.domain.errors import ConversationNotFound
from app.modules.messaging.domain.errors import BodyEmpty, BodyTooLong, ReplyNotInConversation
from app.modules.scheduling.domain.errors import (
    InvalidTimezone,
    NaiveDatetime,
    ScheduledInPast,
    ScheduledTooFar,
)

BACKOFF_BASE = timedelta(seconds=10)
BACKOFF_CAP = timedelta(minutes=15)
TIMEZONE_MAX_LENGTH = 64


class ScheduledStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ScheduledMessage:
    id: UUID
    conversation_id: UUID
    sender_id: UUID
    body: str
    reply_to_id: UUID | None
    client_message_id: UUID
    scheduled_at_utc: datetime
    sender_timezone: str
    status: ScheduledStatus
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
    cancelled_at: datetime | None

    # The state machine (09 §16.2). The SQL guards are what enforce it under
    # concurrency; these say which actions the UI should offer.
    @property
    def can_edit(self) -> bool:
        return self.status is ScheduledStatus.PENDING

    @property
    def can_cancel(self) -> bool:
        return self.status is ScheduledStatus.PENDING

    @property
    def can_retry(self) -> bool:
        return self.status is ScheduledStatus.FAILED


@dataclass(frozen=True, slots=True)
class ScheduledMessageView:
    scheduled: ScheduledMessage
    # The message it produced, found through messages.scheduled_message_id.
    sent_message_id: UUID | None


@dataclass(frozen=True, slots=True)
class ScheduledPage:
    items: list[ScheduledMessageView]
    next_cursor: str | None


# --- validation ----------------------------------------------------------------------


def validate_timezone(name: str) -> str:
    """An IANA zone name ("Asia/Kolkata"), or InvalidTimezone."""
    if not name or len(name) > TIMEZONE_MAX_LENGTH:
        raise InvalidTimezone()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidTimezone() from exc
    return name


def normalize_scheduled_at(
    scheduled_at: datetime, *, now: datetime, min_lead: timedelta, max_horizon: timedelta
) -> datetime:
    """The instant in UTC. It must carry an offset and fall within [now + lead, now + horizon].

    The offset-qualified value is the source of truth; the time zone name is only kept
    for display, so there's no DST guessing here (07 §Scheduled messages).
    """
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        raise NaiveDatetime()
    instant = scheduled_at.astimezone(UTC)
    if instant < now + min_lead:
        raise ScheduledInPast()
    if instant > now + max_horizon:
        raise ScheduledTooFar()
    return instant


# --- retries -------------------------------------------------------------------------


def backoff(attempts: int, *, rng: random.Random | None = None) -> timedelta:
    """Delay before retry number `attempts` (1-based): 10 s doubling, capped at 15 min,
    with ±20% jitter so a burst of failures doesn't retry in lockstep."""
    base: timedelta = min(BACKOFF_BASE * 2 ** max(attempts - 1, 0), BACKOFF_CAP)
    jitter = (rng or random).uniform(0.8, 1.2)
    return base * jitter


@dataclass(frozen=True, slots=True)
class Permanent:
    """Retrying can't help; the row becomes `failed` with this reason."""

    reason: str


@dataclass(frozen=True, slots=True)
class Transient:
    """Retry with backoff, up to the attempt limit. Only the class name is recorded:
    exception messages can contain user data."""

    error_class: str


class PermanentDeliveryError(Exception):
    """Raised by delivery for conditions that are known to be permanent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_PERMANENT_ERRORS: tuple[tuple[type[Exception], str], ...] = (
    # create_message raises this when the sender is no longer an active member.
    (ConversationNotFound, "sender_not_member"),
    (ReplyNotInConversation, "reply_not_in_conversation"),
    (BodyEmpty, "invalid_body"),
    (BodyTooLong, "invalid_body"),
)


def classify_failure(exc: BaseException) -> Permanent | Transient:
    """Sort a delivery failure (09 §17.4). Unknown errors count as transient: database
    hiccups (deadlocks, timeouts) are the common case, and the attempt limit bounds
    the rest."""
    if isinstance(exc, PermanentDeliveryError):
        return Permanent(exc.reason)
    for error_type, reason in _PERMANENT_ERRORS:
        if isinstance(exc, error_type):
            return Permanent(reason)
    return Transient(type(exc).__name__)
