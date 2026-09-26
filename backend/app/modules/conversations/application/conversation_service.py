"""Conversation use cases (docs/design/07 §Conversations, 06 §12, M04).

Authorization is checked here as well as in the route dependencies (defense in depth,
06 §12): services are also called by the worker and the WS gateway, which don't go
through route dependencies. Durable events are published inside the transaction
(ADR-004); until M06 the publisher is a no-op.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.modules.conversations.application.ports import (
    ConversationsUnitOfWork,
    ConversationsUnitOfWorkFactory,
)
from app.modules.conversations.domain.errors import (
    CannotDmSelf,
    CannotRemoveLastOwner,
    ConversationNotFound,
    MemberLimitExceeded,
    MemberNotFound,
    NotAGroup,
    NotOwner,
    UsersNotFound,
)
from app.modules.conversations.domain.model import (
    GROUP_MEMBER_LIMIT,
    Conversation,
    ConversationView,
    MemberProfile,
    MemberRole,
    Membership,
    direct_key,
)
from app.platform.clock import Clock
from app.platform.pagination import InvalidCursor, decode_cursor, encode_cursor

logger = logging.getLogger(__name__)

PREVIEW_SIZE = 5


@dataclass(frozen=True, slots=True)
class ConversationPage:
    items: list[ConversationView]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class AddMembersResult:
    view: ConversationView
    added: list[MemberProfile]
    already_members: list[UUID]


def conversation_payload(view: ConversationView) -> dict[str, Any]:
    """The event payload for a conversation. Viewer-specific fields (my_role,
    my_last_read_seq) are left out because every recipient gets the same payload."""
    conversation = view.conversation
    return {
        "id": str(conversation.id),
        "type": conversation.type.value,
        "title": conversation.title,
        "created_at": conversation.created_at.isoformat(),
        "last_message_seq": conversation.last_message_seq,
        "member_count": view.member_count,
        "members_preview": [member_payload(m) for m in view.members_preview],
    }


def member_payload(member: MemberProfile) -> dict[str, Any]:
    return {
        "user": {
            "id": str(member.user_id),
            "username": member.username,
            "display_name": member.display_name,
        },
        "role": member.role.value,
        "joined_at": member.joined_at.isoformat(),
    }


class ConversationService:
    def __init__(self, uow_factory: ConversationsUnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    # --- helpers ---------------------------------------------------------------------

    @staticmethod
    async def _require_member(
        uow: ConversationsUnitOfWork, conversation_id: UUID, user_id: UUID, *, lock: bool = False
    ) -> tuple[Conversation, Membership]:
        conversation = await uow.conversations.get(conversation_id, for_update=lock)
        membership = await uow.conversations.get_active_membership(conversation_id, user_id)
        if conversation is None or membership is None:
            raise ConversationNotFound()
        return conversation, membership

    @staticmethod
    async def _view(
        uow: ConversationsUnitOfWork, conversation: Conversation, membership: Membership
    ) -> ConversationView:
        summaries = await uow.conversations.member_summaries(
            [conversation.id], viewer_id=membership.user_id, preview_size=PREVIEW_SIZE
        )
        count, preview = summaries[conversation.id]
        return ConversationView(
            conversation=conversation,
            members_preview=preview,
            member_count=count,
            my_role=membership.role,
            my_last_read_seq=membership.last_read_seq,
        )

    async def _reload_view(
        self, uow: ConversationsUnitOfWork, conversation_id: UUID, user_id: UUID
    ) -> ConversationView:
        conversation, membership = await self._require_member(uow, conversation_id, user_id)
        return await self._view(uow, conversation, membership)

    # --- use cases -------------------------------------------------------------------

    async def get_or_create_direct(
        self, *, caller_id: UUID, other_id: UUID
    ) -> tuple[ConversationView, bool]:
        """Race-safe (R-2): concurrent calls from either side get the same conversation."""
        if caller_id == other_id:
            raise CannotDmSelf()
        async with self._uow_factory() as uow:
            if other_id not in await uow.users.active_user_ids([other_id]):
                raise UsersNotFound([other_id])
            conversation_id, created = await uow.conversations.insert_direct_on_conflict(
                direct_key=direct_key(caller_id, other_id), created_by=caller_id
            )
            if created:
                # Same transaction as the winning insert (R-2): no member-less DM exists.
                await uow.conversations.add_member(conversation_id, caller_id, MemberRole.MEMBER)
                await uow.conversations.add_member(conversation_id, other_id, MemberRole.MEMBER)
            view = await self._reload_view(uow, conversation_id, caller_id)
            if created:
                await uow.events.publish(
                    "conversation.created",
                    conversation_id=conversation_id,
                    recipient_user_ids=[caller_id, other_id],
                    payload=conversation_payload(view),
                )
        if created:
            logger.info(
                "Direct conversation created",
                extra={"event": "conversation.created", "conversation_id": str(conversation_id)},
            )
        return view, created

    async def create_group(
        self, *, caller_id: UUID, title: str, member_ids: list[UUID]
    ) -> ConversationView:
        others = list(dict.fromkeys(uid for uid in member_ids if uid != caller_id))
        if len(others) + 1 > GROUP_MEMBER_LIMIT:
            raise MemberLimitExceeded()
        async with self._uow_factory() as uow:
            found = await uow.users.active_user_ids(others)
            missing = [uid for uid in others if uid not in found]
            if missing:
                raise UsersNotFound(missing)
            conversation_id = await uow.conversations.create_group(
                title=title, created_by=caller_id
            )
            await uow.conversations.add_member(conversation_id, caller_id, MemberRole.OWNER)
            for user_id in others:
                await uow.conversations.add_member(conversation_id, user_id, MemberRole.MEMBER)
            view = await self._reload_view(uow, conversation_id, caller_id)
            await uow.events.publish(
                "conversation.created",
                conversation_id=conversation_id,
                recipient_user_ids=[caller_id, *others],
                payload=conversation_payload(view),
            )
            await uow.audit.record(
                "conversation.created",
                actor_user_id=caller_id,
                target_type="conversation",
                target_id=conversation_id,
                metadata={"member_count": len(others) + 1},
            )
        logger.info(
            "Group created",
            extra={"event": "conversation.created", "conversation_id": str(conversation_id)},
        )
        return view

    async def get(self, *, caller_id: UUID, conversation_id: UUID) -> ConversationView:
        async with self._uow_factory() as uow:
            return await self._reload_view(uow, conversation_id, caller_id)

    async def list_members(self, *, caller_id: UUID, conversation_id: UUID) -> list[MemberProfile]:
        async with self._uow_factory() as uow:
            await self._require_member(uow, conversation_id, caller_id)
            return await uow.conversations.list_members(conversation_id)

    async def list_for_user(
        self, *, caller_id: UUID, limit: int, cursor: str | None
    ) -> ConversationPage:
        after: tuple[datetime, UUID] | None = None
        if cursor is not None:
            raw_at, raw_id = decode_cursor(cursor, length=2)
            try:
                after = (datetime.fromisoformat(str(raw_at)), UUID(str(raw_id)))
            except (TypeError, ValueError) as exc:
                raise InvalidCursor() from exc
        async with self._uow_factory() as uow:
            rows = await uow.conversations.list_for_user(caller_id, after=after, limit=limit + 1)
            page, more = rows[:limit], len(rows) > limit
            summaries = await uow.conversations.member_summaries(
                [c.id for c, _ in page], viewer_id=caller_id, preview_size=PREVIEW_SIZE
            )
            latest = await uow.last_messages.latest([(c.id, c.last_message_seq) for c, _ in page])
            unread = await uow.last_messages.unread_counts(
                caller_id, [(c.id, m.last_read_seq) for c, m in page]
            )
        views = [
            ConversationView(
                conversation=conversation,
                members_preview=summaries[conversation.id][1],
                member_count=summaries[conversation.id][0],
                my_role=membership.role,
                my_last_read_seq=membership.last_read_seq,
                last_message=latest.get(conversation.id),
                unread_count=unread.get(conversation.id, 0),
            )
            for conversation, membership in page
        ]
        next_cursor = None
        if more and page:
            last = page[-1][0]
            next_cursor = encode_cursor([last.last_activity_at.isoformat(), str(last.id)])
        return ConversationPage(items=views, next_cursor=next_cursor)

    async def rename(
        self, *, caller_id: UUID, conversation_id: UUID, title: str
    ) -> ConversationView:
        async with self._uow_factory() as uow:
            conversation, membership = await self._require_member(uow, conversation_id, caller_id)
            if not conversation.is_group:
                raise NotAGroup()
            if not membership.is_owner:
                raise NotOwner()
            await uow.conversations.rename(conversation_id, title)
            view = await self._reload_view(uow, conversation_id, caller_id)
            await uow.events.publish(
                "conversation.updated",
                conversation_id=conversation_id,
                recipient_user_ids=await uow.conversations.active_member_ids(conversation_id),
                payload=conversation_payload(view),
            )
        return view

    async def add_members(
        self, *, caller_id: UUID, conversation_id: UUID, user_ids: list[UUID]
    ) -> AddMembersResult:
        requested = list(dict.fromkeys(user_ids))
        async with self._uow_factory() as uow:
            # The conversation row lock serializes concurrent adds, so the member cap
            # can't be exceeded by two requests that each fit on their own.
            conversation, membership = await self._require_member(
                uow, conversation_id, caller_id, lock=True
            )
            if not conversation.is_group:
                raise NotAGroup()
            if not membership.is_owner:
                raise NotOwner()
            found = await uow.users.active_user_ids(requested)
            missing = [uid for uid in requested if uid not in found]
            if missing:
                raise UsersNotFound(missing)

            added_ids: list[UUID] = []
            already: list[UUID] = []
            current = await uow.conversations.count_active_members(conversation_id)
            for user_id in requested:
                if current + len(added_ids) >= GROUP_MEMBER_LIMIT:
                    # Only an error if someone genuinely new would exceed the cap.
                    if await uow.conversations.get_active_membership(conversation_id, user_id):
                        already.append(user_id)
                        continue
                    raise MemberLimitExceeded()
                if await uow.conversations.upsert_member(conversation_id, user_id) is None:
                    already.append(user_id)
                else:
                    added_ids.append(user_id)

            added = await uow.conversations.list_members(conversation_id, user_ids=added_ids)
            view = await self._reload_view(uow, conversation_id, caller_id)
            if added:
                await uow.events.publish(
                    "conversation.member_added",
                    conversation_id=conversation_id,
                    recipient_user_ids=await uow.conversations.active_member_ids(conversation_id),
                    payload={
                        "conversation": conversation_payload(view),
                        "members": [member_payload(m) for m in added],
                    },
                )
                for user_id in added_ids:
                    await uow.audit.record(
                        "conversation.member_added",
                        actor_user_id=caller_id,
                        target_type="conversation",
                        target_id=conversation_id,
                        metadata={"user_id": str(user_id)},
                    )
        return AddMembersResult(view=view, added=added, already_members=already)

    async def remove_member(self, *, caller_id: UUID, conversation_id: UUID, user_id: UUID) -> None:
        """An owner removes a member, or a member leaves (user_id == caller_id)."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            conversation, caller = await self._require_member(uow, conversation_id, caller_id)
            if not conversation.is_group:
                raise NotAGroup()
            leaving = user_id == caller_id
            if not leaving and not caller.is_owner:
                raise NotOwner()
            target = await uow.conversations.get_active_membership(conversation_id, user_id)
            if target is None:
                raise MemberNotFound()

            if target.is_owner:
                # R-12: lock the owner rows first, then check. Concurrent removals run one
                # after the other, and the second sees one owner left.
                owners = await uow.conversations.lock_owner_rows(conversation_id)
                if user_id in owners and len(owners) <= 1:
                    raise CannotRemoveLastOwner()
                if not leaving and caller_id not in owners:
                    # Removed as owner by a concurrent request while we waited.
                    raise NotOwner()

            if not await uow.conversations.mark_left(conversation_id, user_id, now=now):
                raise MemberNotFound()
            remaining = await uow.conversations.active_member_ids(conversation_id)
            await uow.events.publish(
                "conversation.member_removed",
                conversation_id=conversation_id,
                # The removed user is told too, so their client drops the conversation.
                recipient_user_ids=[*remaining, user_id],
                payload={
                    "conversation_id": str(conversation_id),
                    "user_id": str(user_id),
                    "removed_by": str(caller_id),
                },
            )
            await uow.audit.record(
                "conversation.member_left" if leaving else "conversation.member_removed",
                actor_user_id=caller_id,
                target_type="conversation",
                target_id=conversation_id,
                metadata={"user_id": str(user_id)},
            )

    async def set_muted(
        self, *, caller_id: UUID, conversation_id: UUID, muted: bool
    ) -> MemberProfile:
        async with self._uow_factory() as uow:
            await self._require_member(uow, conversation_id, caller_id)
            await uow.conversations.set_muted(conversation_id, caller_id, muted)
            [member] = await uow.conversations.list_members(conversation_id, user_ids=[caller_id])
        return member
