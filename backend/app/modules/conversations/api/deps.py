"""Conversation dependencies: service wiring and the reusable membership checks."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request

from app.modules.conversations.application.conversation_service import ConversationService
from app.modules.conversations.domain.errors import ConversationNotFound, NotOwner
from app.modules.conversations.domain.model import Membership
from app.modules.conversations.infrastructure.repository import ConversationRepository
from app.modules.conversations.infrastructure.unit_of_work import SqlConversationsUnitOfWork
from app.modules.identity.api.deps import Authenticated
from app.platform.db import UnitOfWork


def get_conversation_service(request: Request) -> ConversationService:
    state = request.app.state
    return ConversationService(
        lambda: SqlConversationsUnitOfWork(state.session_factory), state.clock
    )


Conversations = Annotated[ConversationService, Depends(get_conversation_service)]


async def require_active_member(
    conversation_id: UUID, request: Request, current: Authenticated
) -> Membership:
    """The caller's active membership, or 404 (non-members and former members alike,
    so existence isn't revealed; docs/design/06 §12). Every conversation-scoped route
    uses this; the service repeats the check inside its own transaction."""
    async with UnitOfWork(request.app.state.session_factory) as uow:
        membership = await ConversationRepository(uow.session).get_active_membership(
            conversation_id, current.user.id
        )
    if membership is None:
        raise ConversationNotFound()
    return membership


ActiveMember = Annotated[Membership, Depends(require_active_member)]


async def require_owner(membership: ActiveMember) -> Membership:
    if not membership.is_owner:
        raise NotOwner()
    return membership
