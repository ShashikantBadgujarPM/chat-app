"""JSON payloads for messages, shared by the REST responses and the WS events.

`message.created` / `message.updated` carry the REST `Message` schema (docs/design/08
§15.4). Building both from this one function keeps them from drifting apart: the API
schema is validated from this payload.
"""

from datetime import UTC, datetime
from typing import Any

from app.modules.messaging.domain.message import Message, MessageView


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def message_brief(message: Message) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "seq": message.seq,
        "sender_id": str(message.sender_id) if message.sender_id else None,
        "body_preview": message.preview,
        "deleted": message.is_deleted,
    }


def message_payload(view: MessageView) -> dict[str, Any]:
    message = view.message
    sender = view.sender
    return {
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "seq": message.seq,
        "sender": (
            {
                "id": str(sender.id),
                "username": sender.username,
                "display_name": sender.display_name,
                # Live presence arrives in M08.
                "presence": {"status": "offline", "last_seen_at": None},
            }
            if sender is not None
            else None
        ),
        "body": message.body,
        "reply_to": message_brief(view.reply_to) if view.reply_to is not None else None,
        "mentions": [str(user_id) for user_id in view.mentions],
        "created_at": iso_utc(message.created_at),
        "edited_at": iso_utc(message.edited_at),
        "deleted_at": iso_utc(message.deleted_at),
        "scheduled_message_id": (
            str(message.scheduled_message_id) if message.scheduled_message_id else None
        ),
        "client_message_id": str(message.client_message_id),
    }


def message_deleted_payload(message: Message) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "seq": message.seq,
        "deleted_at": iso_utc(message.deleted_at),
    }
