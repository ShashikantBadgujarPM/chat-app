"""The ScheduledMessage JSON shape, shared by REST responses and `scheduled_message.*`
WS events (docs/design/07 §Scheduled messages, 08 §15.4)."""

from typing import Any

from app.modules.messaging.application.payloads import iso_utc
from app.modules.scheduling.domain.scheduled_message import ScheduledMessageView


def scheduled_payload(view: ScheduledMessageView) -> dict[str, Any]:
    s = view.scheduled
    return {
        "id": str(s.id),
        "conversation_id": str(s.conversation_id),
        "body": s.body,
        "reply_to_id": str(s.reply_to_id) if s.reply_to_id else None,
        "scheduled_at": iso_utc(s.scheduled_at_utc),
        "timezone": s.sender_timezone,
        "status": s.status.value,
        "attempts": s.attempts,
        "last_error": s.last_error,
        "sent_message_id": str(view.sent_message_id) if view.sent_message_id else None,
        "sent_at": iso_utc(s.sent_at),
        "created_at": iso_utc(s.created_at),
        "updated_at": iso_utc(s.updated_at),
        "cancelled_at": iso_utc(s.cancelled_at),
    }
