"""Messaging use cases (docs/design/07 §Messages, M05).

`MessagingService.create_message` is **the single write path** for messages
(docs/design/15 §26.4 #5). It runs inside a caller-supplied UnitOfWork and knows
nothing about HTTP, so the scheduled-message worker (M11) calls the same code the REST
endpoint does.
"""

import logging
from collections.abc import AsyncIterator
from uuid import UUID

from app.modules.conversations.domain.errors import ConversationNotFound
from app.modules.messaging.application.payloads import message_deleted_payload, message_payload
from app.modules.messaging.application.ports import (
    MessagingUnitOfWork,
    MessagingUnitOfWorkFactory,
)
from app.modules.messaging.domain.errors import (
    ConflictingCursors,
    MessageDeleted,
    MessageNotFound,
    NotSender,
    ReplyNotInConversation,
)
from app.modules.messaging.domain.message import (
    AroundPage,
    HistoryPage,
    Message,
    MessageView,
    normalize_body,
)
from app.platform.clock import Clock

logger = logging.getLogger(__name__)


class _ConcurrentReplay(Exception):
    """Internal: another request with the same client_message_id won the insert."""


class MessagingService:
    def __init__(self, uow_factory: MessagingUnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    # --- the write path ----------------------------------------------------------------

    async def create_message(
        self,
        uow: MessagingUnitOfWork,
        *,
        sender_id: UUID,
        conversation_id: UUID,
        body: str,
        reply_to_id: UUID | None,
        client_message_id: UUID,
        scheduled_message_id: UUID | None = None,
    ) -> tuple[MessageView, bool]:
        """Create a message in the caller's transaction. Returns (view, created).

        Idempotent (R-5): a repeated client_message_id returns the original message
        with created=False, and never consumes a seq, so seqs stay gap-free.
        Raises ConversationNotFound if the sender isn't an active member.
        """
        text = normalize_body(body)
        if await uow.conversations.get_active_membership(conversation_id, sender_id) is None:
            raise ConversationNotFound()

        existing = await uow.messages.get_by_client_id(sender_id, client_message_id)
        if existing is not None:
            return (await uow.messages.load_views([existing]))[0], False

        if reply_to_id is not None:
            target = await uow.messages.get(reply_to_id)
            # A deleted target is fine: the reply shows a tombstone preview.
            if target is None or target.conversation_id != conversation_id:
                raise ReplyNotInConversation()

        message: Message | None = None
        try:
            # seq and insert together in a savepoint: if a concurrent replay wins the
            # unique constraint, rolling back also undoes our seq increment.
            async with uow.savepoint():
                seq = await uow.messages.next_seq(conversation_id)
                message = await uow.messages.insert_on_conflict_client_id(
                    conversation_id=conversation_id,
                    seq=seq,
                    sender_id=sender_id,
                    body=text,
                    reply_to_id=reply_to_id,
                    client_message_id=client_message_id,
                    scheduled_message_id=scheduled_message_id,
                )
                if message is None:
                    raise _ConcurrentReplay()
        except _ConcurrentReplay:
            winner = await uow.messages.get_by_client_id(sender_id, client_message_id)
            assert winner is not None  # noqa: S101 - the conflict proves it exists
            return (await uow.messages.load_views([winner]))[0], False

        view = (await uow.messages.load_views([message]))[0]
        await uow.events.publish(
            "message.created",
            conversation_id=conversation_id,
            recipient_user_ids=await uow.conversations.active_member_ids(conversation_id),
            payload=message_payload(view),
        )
        logger.info(
            "Message created",
            extra={
                "event": "message.created",
                "message_id": str(message.id),
                "conversation_id": str(conversation_id),
                "seq": message.seq,
                "body_length": len(text),  # never the body itself
            },
        )
        return view, True

    # --- REST-facing use cases (own their UoW) -----------------------------------------

    async def send(
        self,
        *,
        sender_id: UUID,
        conversation_id: UUID,
        body: str,
        reply_to_id: UUID | None,
        client_message_id: UUID,
    ) -> tuple[MessageView, bool]:
        async with self._uow_factory() as uow:
            return await self.create_message(
                uow,
                sender_id=sender_id,
                conversation_id=conversation_id,
                body=body,
                reply_to_id=reply_to_id,
                client_message_id=client_message_id,
            )

    @staticmethod
    async def _visible_message(
        uow: MessagingUnitOfWork, message_id: UUID, caller_id: UUID
    ) -> Message:
        """The message, if the caller is an active member of its conversation; else 404."""
        message = await uow.messages.get(message_id)
        if message is None or (
            await uow.conversations.get_active_membership(message.conversation_id, caller_id)
            is None
        ):
            raise MessageNotFound()
        return message

    async def get_message(self, *, caller_id: UUID, message_id: UUID) -> MessageView:
        async with self._uow_factory() as uow:
            message = await self._visible_message(uow, message_id, caller_id)
            return (await uow.messages.load_views([message]))[0]

    async def edit_message(self, *, caller_id: UUID, message_id: UUID, body: str) -> MessageView:
        text = normalize_body(body)
        async with self._uow_factory() as uow:
            message = await self._visible_message(uow, message_id, caller_id)
            if message.sender_id != caller_id:
                raise NotSender()
            if message.is_deleted:
                raise MessageDeleted()
            # R-13: guarded in SQL; a delete that committed first makes this match nothing.
            edited = await uow.messages.edit(message_id, sender_id=caller_id, body=text)
            if edited is None:
                raise MessageDeleted()
            view = (await uow.messages.load_views([edited]))[0]
            await uow.events.publish(
                "message.updated",
                conversation_id=edited.conversation_id,
                recipient_user_ids=await uow.conversations.active_member_ids(
                    edited.conversation_id
                ),
                payload=message_payload(view),
            )
        logger.info(
            "Message updated",
            extra={
                "event": "message.updated",
                "message_id": str(message_id),
                "conversation_id": str(edited.conversation_id),
                "seq": edited.seq,
                "body_length": len(text),
            },
        )
        return view

    async def delete_message(self, *, caller_id: UUID, message_id: UUID) -> None:
        """Soft delete. Idempotent: deleting a deleted message succeeds quietly."""
        async with self._uow_factory() as uow:
            message = await self._visible_message(uow, message_id, caller_id)
            if message.sender_id != caller_id:
                raise NotSender()
            deleted = await uow.messages.soft_delete(message_id, sender_id=caller_id)
            if deleted is None:
                return  # already a tombstone (possibly deleted concurrently)
            await uow.events.publish(
                "message.deleted",
                conversation_id=deleted.conversation_id,
                recipient_user_ids=await uow.conversations.active_member_ids(
                    deleted.conversation_id
                ),
                payload=message_deleted_payload(deleted),
            )
        logger.info(
            "Message deleted",
            extra={
                "event": "message.deleted",
                "message_id": str(message_id),
                "conversation_id": str(deleted.conversation_id),
                "seq": deleted.seq,
            },
        )

    # --- history -------------------------------------------------------------------------

    @staticmethod
    async def _require_member(
        uow: MessagingUnitOfWork, conversation_id: UUID, user_id: UUID
    ) -> None:
        if await uow.conversations.get_active_membership(conversation_id, user_id) is None:
            raise ConversationNotFound()

    async def history(
        self,
        *,
        caller_id: UUID,
        conversation_id: UUID,
        before_seq: int | None,
        after_seq: int | None,
        limit: int,
    ) -> HistoryPage:
        """Latest (neither cursor) and before_seq pages are newest first; after_seq is
        oldest first (docs/design/07 §Messages)."""
        if before_seq is not None and after_seq is not None:
            raise ConflictingCursors()
        async with self._uow_factory() as uow:
            await self._require_member(uow, conversation_id, caller_id)
            if after_seq is not None:
                rows = await uow.messages.page_after(
                    conversation_id, after_seq=after_seq, limit=limit + 1
                )
            else:
                rows = await uow.messages.page_before(
                    conversation_id, before_seq=before_seq, limit=limit + 1
                )
            views = await uow.messages.load_views(rows[:limit])
        return HistoryPage(items=views, has_more=len(rows) > limit)

    async def around(
        self, *, caller_id: UUID, conversation_id: UUID, seq: int, limit: int
    ) -> AroundPage:
        """Jump to a message: up to `limit` messages centred on `seq`, oldest first."""
        before_count = limit // 2
        after_count = limit - before_count  # includes `seq` itself
        async with self._uow_factory() as uow:
            await self._require_member(uow, conversation_id, caller_id)
            older = await uow.messages.page_before(
                conversation_id, before_seq=seq, limit=before_count + 1
            )
            newer = await uow.messages.page_after(
                conversation_id, after_seq=seq - 1, limit=after_count + 1
            )
            items = list(reversed(older[:before_count])) + newer[:after_count]
            views = await uow.messages.load_views(items)
        return AroundPage(
            items=views,
            has_more_before=len(older) > before_count,
            has_more_after=len(newer) > after_count,
        )

    async def iter_history(
        self, *, caller_id: UUID, conversation_id: UUID, page_size: int = 100
    ) -> AsyncIterator[list[MessageView]]:
        """Walk a conversation's whole history, newest first, one page at a time.

        An async generator for internal consumers (exports, tests): each page is read
        in its own short transaction, so walking a long history never holds one open.
        """
        before: int | None = None
        while True:
            page = await self.history(
                caller_id=caller_id,
                conversation_id=conversation_id,
                before_seq=before,
                after_seq=None,
                limit=page_size,
            )
            if page.items:
                yield page.items
            if not page.has_more:
                return
            before = page.items[-1].message.seq
