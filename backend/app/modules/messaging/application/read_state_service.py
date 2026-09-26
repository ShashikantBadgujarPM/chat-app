"""Read cursors, unread counts and read receipts (docs/design/07 §Conversations, M07).

Read state is one monotonic cursor per member (ADR-003): marking read is a single
GREATEST update, and the unread count is an index range count from the cursor.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from app.modules.conversations.domain.errors import ConversationNotFound
from app.modules.conversations.domain.model import ConversationType
from app.modules.messaging.application.ports import MessagingUnitOfWorkFactory
from app.platform.errors import ValidationError

logger = logging.getLogger(__name__)

# Receipts fan out to every other member; above this size they aren't sent, which keeps
# the fan-out of one read bounded (docs/design/07 §Conversations).
RECEIPT_GROUP_LIMIT = 20


class CursorBeyondLatest(ValidationError):
    default_code = "cursor_beyond_latest"
    default_message = "last_read_seq is past the conversation's latest message."


@dataclass(frozen=True, slots=True)
class ReadState:
    last_read_seq: int
    unread_count: int


def receipts_enabled(conversation_type: ConversationType, member_count: int) -> bool:
    return conversation_type is ConversationType.DIRECT or member_count < RECEIPT_GROUP_LIMIT


class ReadStateService:
    def __init__(self, uow_factory: MessagingUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def mark_read(
        self, *, user_id: UUID, conversation_id: UUID, last_read_seq: int
    ) -> ReadState:
        async with self._uow_factory() as uow:
            conversation = await uow.conversations.get(conversation_id)
            membership = await uow.conversations.get_active_membership(conversation_id, user_id)
            if conversation is None or membership is None:
                raise ConversationNotFound()
            if last_read_seq > conversation.last_message_seq:
                raise CursorBeyondLatest(
                    details={"last_message_seq": conversation.last_message_seq}
                )

            stored, moved = await uow.conversations.advance_read_cursor(
                conversation_id, user_id, last_read_seq
            )
            unread = (await uow.messages.unread_counts(user_id, [(conversation_id, stored)]))[
                conversation_id
            ]
            if moved:
                # Badge sync across the reader's own tabs.
                await uow.events.publish(
                    "conversation.read",
                    conversation_id=conversation_id,
                    recipient_user_ids=[user_id],
                    payload={
                        "conversation_id": str(conversation_id),
                        "last_read_seq": stored,
                        "unread_count": unread,
                        "unread_mention_count": 0,  # mentions arrive in M10
                    },
                )
                members = await uow.conversations.active_member_ids(conversation_id)
                if receipts_enabled(conversation.type, len(members)):
                    others = [m for m in members if m != user_id]
                    await uow.events.publish(
                        "receipt.updated",
                        conversation_id=conversation_id,
                        recipient_user_ids=others,
                        payload={
                            "conversation_id": str(conversation_id),
                            "user_id": str(user_id),
                            "last_read_seq": stored,
                        },
                    )
        return ReadState(last_read_seq=stored, unread_count=unread)
