"""Messages: nested under conversations for list/create, top-level for item operations
(docs/design/07 §13.2)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.modules.conversations.api.deps import ActiveMember
from app.modules.identity.api.deps import Authenticated, authenticated_route_dependencies
from app.modules.messaging.api.deps import Messaging, send_rate_limit
from app.modules.messaging.api.schemas import (
    AroundResponse,
    EditMessageRequest,
    HistoryResponse,
    MessageOut,
    SendMessageRequest,
)

Limit = Annotated[int, Query(ge=1, le=100)]

conversation_messages_router = APIRouter(
    prefix="/api/v1/conversations/{conversation_id}/messages",
    tags=["messages"],
    dependencies=authenticated_route_dependencies,
)


@conversation_messages_router.get("")
async def list_messages(
    conversation_id: UUID,
    member: ActiveMember,
    messaging: Messaging,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    limit: Limit = 50,
) -> HistoryResponse:
    page = await messaging.history(
        caller_id=member.user_id,
        conversation_id=conversation_id,
        before_seq=before_seq,
        after_seq=after_seq,
        limit=limit,
    )
    return HistoryResponse(
        items=[MessageOut.from_view(v) for v in page.items], has_more=page.has_more
    )


@conversation_messages_router.get("/around/{seq}")
async def messages_around(
    conversation_id: UUID,
    seq: Annotated[int, Path(ge=1)],
    member: ActiveMember,
    messaging: Messaging,
    limit: Limit = 50,
) -> AroundResponse:
    page = await messaging.around(
        caller_id=member.user_id, conversation_id=conversation_id, seq=seq, limit=limit
    )
    return AroundResponse(
        items=[MessageOut.from_view(v) for v in page.items],
        has_more_before=page.has_more_before,
        has_more_after=page.has_more_after,
    )


@conversation_messages_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(send_rate_limit)],
    responses={200: {"description": "Idempotent replay: the original message"}},
)
async def send_message(
    conversation_id: UUID,
    body: SendMessageRequest,
    response: Response,
    member: ActiveMember,
    messaging: Messaging,
) -> MessageOut:
    view, created = await messaging.send(
        sender_id=member.user_id,
        conversation_id=conversation_id,
        body=body.body,
        reply_to_id=body.reply_to_id,
        client_message_id=body.client_message_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return MessageOut.from_view(view)


messages_router = APIRouter(
    prefix="/api/v1/messages", tags=["messages"], dependencies=authenticated_route_dependencies
)


@messages_router.get("/{message_id}")
async def get_message(message_id: UUID, current: Authenticated, messaging: Messaging) -> MessageOut:
    view = await messaging.get_message(caller_id=current.user.id, message_id=message_id)
    return MessageOut.from_view(view)


@messages_router.patch("/{message_id}")
async def edit_message(
    message_id: UUID, body: EditMessageRequest, current: Authenticated, messaging: Messaging
) -> MessageOut:
    view = await messaging.edit_message(
        caller_id=current.user.id, message_id=message_id, body=body.body
    )
    return MessageOut.from_view(view)


@messages_router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(message_id: UUID, current: Authenticated, messaging: Messaging) -> None:
    await messaging.delete_message(caller_id=current.user.id, message_id=message_id)
