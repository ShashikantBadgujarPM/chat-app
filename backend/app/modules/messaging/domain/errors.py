"""Messaging errors. Codes follow docs/design/07 §Messages."""

from app.platform.errors import (
    AuthorizationError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationError,
)


class MessageNotFound(NotFoundError):
    # Also for messages in conversations the caller can't see (docs/design/06 §12).
    default_code = "message_not_found"
    default_message = "The message was not found."


class NotSender(AuthorizationError):
    default_code = "not_sender"
    default_message = "Only the sender can change this message."


class MessageDeleted(ConflictError):
    default_code = "message_deleted"
    default_message = "The message was deleted."


class ReplyNotInConversation(ValidationError):
    default_code = "reply_not_in_conversation"
    default_message = "You can only reply to a message in the same conversation."


class BodyEmpty(ValidationError):
    default_code = "body_empty"
    default_message = "The message is empty."


class BodyTooLong(ValidationError):
    default_code = "body_too_long"
    default_message = "The message is longer than 4000 characters."


class ConflictingCursors(BadRequestError):
    default_code = "conflicting_cursors"
    default_message = "Use at most one of before_seq and after_seq."
