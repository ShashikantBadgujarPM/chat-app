"""Scheduling errors. Codes follow docs/design/07 §Scheduled messages."""

from app.platform.errors import ConflictError, NotFoundError, RateLimitedError, ValidationError


class ScheduledMessageNotFound(NotFoundError):
    # Also for other users' scheduled messages: they're private to the sender.
    default_code = "scheduled_message_not_found"
    default_message = "The scheduled message was not found."


class NotPending(ConflictError):
    default_code = "not_pending"
    default_message = "The scheduled message was already sent, cancelled or failed."


class NotFailed(ConflictError):
    default_code = "not_failed"
    default_message = "Only a failed scheduled message can be retried."


class NaiveDatetime(ValidationError):
    default_code = "naive_datetime"
    default_message = "scheduled_at must include a UTC offset, e.g. 2026-10-01T18:00:00+05:30."


class ScheduledInPast(ValidationError):
    default_code = "scheduled_in_past"
    default_message = "The scheduled time must be at least 30 seconds from now."


class ScheduledTooFar(ValidationError):
    default_code = "scheduled_too_far"
    default_message = "The scheduled time must be at most a year from now."


class InvalidTimezone(ValidationError):
    default_code = "invalid_timezone"
    default_message = "The time zone is not a valid IANA name, e.g. Asia/Kolkata."


class TooManyPending(RateLimitedError):
    default_code = "too_many_pending"
    default_message = "You have too many pending scheduled messages."
