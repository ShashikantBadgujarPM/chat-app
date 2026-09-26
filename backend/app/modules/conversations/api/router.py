"""/api/v1/conversations (docs/design/07 §Conversations)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from app.modules.conversations.api.deps import ActiveMember, Conversations
from app.modules.conversations.api.schemas import (
    AddMembersRequest,
    AddMembersResponse,
    ConversationOut,
    ConversationSummaryOut,
    CreateDirectRequest,
    CreateGroupRequest,
    MemberOut,
    MembershipOut,
    MembershipUpdateRequest,
    MembersResponse,
    ReadCursorRequest,
    ReadCursorResponse,
    RenameRequest,
)
from app.modules.identity.api.deps import Authenticated, authenticated_route_dependencies
from app.modules.messaging.application.read_state_service import (
    ReadStateService,
    receipts_enabled,
)
from app.modules.messaging.infrastructure.unit_of_work import SqlMessagingUnitOfWork
from app.platform.pagination import Page

router = APIRouter(
    prefix="/api/v1/conversations",
    tags=["conversations"],
    dependencies=authenticated_route_dependencies,
)

# Routes with a conversation id depend on ActiveMember (404 for non-members). Group and
# owner rules are checked by the service, which also reports not_a_group before
# not_owner for direct conversations.


@router.get("")
async def list_conversations(
    current: Authenticated,
    service: Conversations,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> Page[ConversationSummaryOut]:
    page = await service.list_for_user(caller_id=current.user.id, limit=limit, cursor=cursor)
    return Page(
        items=[ConversationSummaryOut.from_view(v) for v in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/direct", responses={200: {"description": "The conversation already existed"}})
async def create_direct(
    body: CreateDirectRequest, response: Response, current: Authenticated, service: Conversations
) -> ConversationOut:
    view, created = await service.get_or_create_direct(
        caller_id=current.user.id, other_id=body.user_id
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return ConversationOut.from_view(view)


@router.post("/groups", status_code=status.HTTP_201_CREATED)
async def create_group(
    body: CreateGroupRequest, current: Authenticated, service: Conversations
) -> ConversationOut:
    view = await service.create_group(
        caller_id=current.user.id, title=body.title, member_ids=body.member_ids
    )
    return ConversationOut.from_view(view)


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: UUID, member: ActiveMember, service: Conversations
) -> ConversationOut:
    view = await service.get(caller_id=member.user_id, conversation_id=conversation_id)
    return ConversationOut.from_view(view)


@router.patch("/{conversation_id}")
async def rename_conversation(
    conversation_id: UUID, body: RenameRequest, member: ActiveMember, service: Conversations
) -> ConversationOut:
    view = await service.rename(
        caller_id=member.user_id, conversation_id=conversation_id, title=body.title
    )
    return ConversationOut.from_view(view)


@router.get("/{conversation_id}/members")
async def list_members(
    conversation_id: UUID, member: ActiveMember, service: Conversations
) -> MembersResponse:
    view = await service.get(caller_id=member.user_id, conversation_id=conversation_id)
    members = await service.list_members(caller_id=member.user_id, conversation_id=conversation_id)
    receipts = receipts_enabled(view.conversation.type, view.member_count)
    return MembersResponse(items=[MemberOut.from_profile(m, receipts=receipts) for m in members])


@router.put("/{conversation_id}/read-cursor")
async def put_read_cursor(
    conversation_id: UUID,
    body: ReadCursorRequest,
    member: ActiveMember,
    request: Request,
) -> ReadCursorResponse:
    service = ReadStateService(lambda: SqlMessagingUnitOfWork(request.app.state.session_factory))
    state = await service.mark_read(
        user_id=member.user_id, conversation_id=conversation_id, last_read_seq=body.last_read_seq
    )
    return ReadCursorResponse(last_read_seq=state.last_read_seq, unread_count=state.unread_count)


@router.post("/{conversation_id}/members")
async def add_members(
    conversation_id: UUID, body: AddMembersRequest, member: ActiveMember, service: Conversations
) -> AddMembersResponse:
    result = await service.add_members(
        caller_id=member.user_id, conversation_id=conversation_id, user_ids=body.user_ids
    )
    return AddMembersResponse(
        added=[MemberOut.from_profile(m) for m in result.added],
        already_members=result.already_members,
    )


@router.delete("/{conversation_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    conversation_id: UUID, user_id: UUID, member: ActiveMember, service: Conversations
) -> None:
    await service.remove_member(
        caller_id=member.user_id, conversation_id=conversation_id, user_id=user_id
    )


@router.patch("/{conversation_id}/membership")
async def update_membership(
    conversation_id: UUID,
    body: MembershipUpdateRequest,
    member: ActiveMember,
    service: Conversations,
) -> MembershipOut:
    updated = await service.set_muted(
        caller_id=member.user_id, conversation_id=conversation_id, muted=body.notifications_muted
    )
    return MembershipOut.from_profile(updated)
