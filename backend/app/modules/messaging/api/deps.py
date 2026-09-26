"""Messaging service wiring and the per-user send limit."""

from typing import Annotated

from fastapi import Depends, Request

from app.modules.messaging.application.messaging_service import MessagingService
from app.modules.messaging.infrastructure.unit_of_work import SqlMessagingUnitOfWork
from app.platform.rate_limit import rate_limited


def get_messaging_service(request: Request) -> MessagingService:
    state = request.app.state
    return MessagingService(lambda: SqlMessagingUnitOfWork(state.session_factory), state.clock)


Messaging = Annotated[MessagingService, Depends(get_messaging_service)]

# 30 sends per 10 s per user, burst-friendly (docs/design/07 §13.4). The key is set by
# get_current_user, which the router's dependencies resolve first.
send_rate_limit = rate_limited(
    "messages.send",
    lambda r: getattr(r.state, "user_id", None),
    limit=lambda r: (r.app.state.settings.rate_limit_message_send_per_10s, 10),
)
