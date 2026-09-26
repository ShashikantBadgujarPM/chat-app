# M05 — Messages over REST

## Goal
Implement messaging over REST only: send (idempotent through `client_message_id`), per-conversation `seq` assignment that's safe under concurrency, edit and soft-delete by the sender, replies within the same conversation, keyset-paginated history (latest, before, after, around), and `last_message` on the conversation list. This is the most important write path in the system, and every later feature (realtime, unread, mentions, scheduled delivery) builds on the `MessagingService.create_message` function defined here.

## Dependencies
M04.

## Files/modules expected
- `backend/app/modules/messaging/domain/`: `Message` dataclass (`is_deleted`, `can_be_edited_by`), body normalization and limits, exceptions (`NotSender`, `MessageDeleted`, `ReplyNotInConversation`)
- `backend/app/modules/messaging/application/messaging_service.py`:
  - `create_message(uow, *, sender_id, conversation_id, body, reply_to_id, client_message_id, scheduled_message_id=None)`, **the single write path**, also called by the worker in M11; it is written to run inside a caller-supplied UoW, not to own one
  - `edit_message`, `delete_message`, `get_message`
  - `iter_history(...)`, an **async generator** yielding pages for internal consumers (export, tests)
- `backend/app/modules/messaging/infrastructure/`: ORM model, repository (`next_seq(conversation_id)` via `UPDATE … RETURNING`, `insert_on_conflict_client_id`, `page_before`, `page_after`, `page_around`)
- `backend/app/modules/messaging/api/`: `conversation_messages_router` (nested list and create) and `messages_router` (item operations), schemas `Message` and `MessageBrief`
- `frontend/src/features/messages/`: message list with infinite scroll upward (`before_seq`), a composer (generates `client_message_id`, shows an optimistic pending state, retries with the same id), edit and delete actions on own messages, a reply preview, a "This message was deleted" tombstone

## Database changes
- `messages` as in [05](../design/05-database.md), including `UNIQUE(conversation_id, seq)`, `UNIQUE(sender_id, client_message_id)`, `INDEX(conversation_id, seq DESC)` and the `reply_to_id` index.
- `scheduled_message_id` is added **now** as a nullable UUID column without the FK. M11 adds the FK and the unique constraint once `scheduled_messages` exists. The `search_vector` generated column and its GIN index are deferred to M12.

## API changes
From [07 §Messages](../design/07-rest-api.md): `GET /conversations/{id}/messages`, `GET /conversations/{id}/messages/around/{seq}`, `POST /conversations/{id}/messages`, `GET /messages/{id}`, `PATCH /messages/{id}`, `DELETE /messages/{id}`. `GET /conversations` now fills in `last_message` and sorts by `last_activity_at`. Mentions are returned as `[]` until M10.

## WebSocket changes
None. `create_message`, `edit_message` and `delete_message` already call `EventPublisher.publish(event)` with the full event, including `recipient_user_ids` computed from the active members inside the transaction. The publisher is still the M04 no-op.

## Tests
- Integration (R-1): 50 concurrent sends into one conversation → seqs are exactly 1..50; 50 concurrent sends spread over 5 conversations don't serialize on each other (a timing sanity check, not a strict assertion).
- API (R-5): the same `client_message_id` sent twice → 201, then 200 with the same body and id; a different body with the same id → still 200 with the *original* (documented behavior).
- API: a reply to a message in another conversation → 422; a reply to a deleted message is allowed (the reply shows a tombstone preview).
- API (R-13): editing a deleted message → 409; a concurrent edit and delete end in a consistent state; delete is idempotent.
- API: a non-sender editing or deleting → 403; a non-member doing anything → 404; a former member → 404.
- API: history paging: latest page, `before_seq` down to seq 1 with `has_more=false` at the end, `after_seq`, `around`, both cursors → 400, `limit` over 100 → 422.
- API: body trimming; a whitespace-only body → 422 `body_empty`; 4001 characters → 422 `body_too_long`.
- Unit: an application service test with a fake publisher asserts the event type, payload and `recipient_user_ids` for create, edit and delete.

## Acceptance criteria
- Users in a conversation can exchange, edit, delete and reply to messages. At this stage the UI updates on refresh or navigation; realtime comes in M06.
- Scrolling up loads older history smoothly, and `EXPLAIN` shows an index scan on `(conversation_id, seq)`.
- Double-clicking send or retrying on network failure never produces a duplicate message.
- `create_message` has no knowledge of HTTP and can be called with any open UoW.
