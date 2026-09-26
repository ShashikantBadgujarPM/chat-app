"""Conversation errors. Codes follow docs/design/07 §Conversations."""

from uuid import UUID

from app.modules.conversations.domain.model import GROUP_MEMBER_LIMIT
from app.platform.errors import (
    AuthorizationError,
    InvariantViolation,
    NotFoundError,
    ValidationError,
)


class ConversationNotFound(NotFoundError):
    # Also for conversations the caller isn't (or is no longer) a member of: 404, not
    # 403, so existence isn't revealed (docs/design/06 §12).
    default_code = "conversation_not_found"
    default_message = "The conversation was not found."


class MemberNotFound(NotFoundError):
    default_code = "member_not_found"
    default_message = "That user is not a member of this conversation."


class UsersNotFound(NotFoundError):
    default_code = "user_not_found"
    default_message = "One or more users were not found."

    def __init__(self, user_ids: list[UUID]) -> None:
        super().__init__(details={"user_ids": [str(user_id) for user_id in user_ids]})


class NotOwner(AuthorizationError):
    default_code = "not_owner"
    default_message = "Only a group owner can do that."


class NotAGroup(ValidationError):
    default_code = "not_a_group"
    default_message = "That is only possible in a group conversation."


class CannotDmSelf(ValidationError):
    default_code = "cannot_dm_self"
    default_message = "You can't start a direct conversation with yourself."


class MemberLimitExceeded(ValidationError):
    default_code = "member_limit_exceeded"
    default_message = f"A group can have at most {GROUP_MEMBER_LIMIT} members."


class CannotRemoveLastOwner(InvariantViolation):
    default_code = "cannot_remove_last_owner"
    default_message = "A group must keep at least one owner."
