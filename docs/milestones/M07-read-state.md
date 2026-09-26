# M07 — Read State, Unread Counts, Read Receipts

## Goal
Implement read cursors per member: a monotonic `PUT read-cursor`, server-computed `unread_count` in the conversation list, `conversation.read` so badges stay in sync across the reader's tabs, and `receipt.updated` for direct and small group conversations. The frontend marks a conversation read when it's open and visible, and keeps unread badges up to date for conversations that aren't open (the "receiver in another conversation" scenario).

## Dependencies
M06.

## Files/modules expected
- `backend/app/modules/messaging/application/read_state_service.py`: `mark_read(uow, user_id, conversation_id, last_read_seq)` → `GREATEST` update, a recomputed unread count, and the `conversation.read` event (recipients: the reader only) plus `receipt.updated` (recipients: the other members, when the conversation is direct or has fewer than 20 members)
- `backend/app/modules/conversations/infrastructure/conversation_repository.py`: `list_for_user` extended with an `unread_count` subquery (`COUNT(*) FROM messages WHERE conversation_id = c.id AND seq > m.last_read_seq AND sender_id <> :me AND deleted_at IS NULL`)
- `backend/app/modules/conversations/api/`: `PUT /conversations/{id}/read-cursor`
- `frontend`: an "is visible and focused" hook (Page Visibility API + window focus + the conversation being open + scrolled near the bottom), a debounced (500 ms) read-cursor PUT, unread badges in the sidebar, a "Seen" indicator under the last message in DMs

## Database changes
None. It uses `conversation_members.last_read_seq` from M04 and the `messages` index from M05. Add a migration only if `EXPLAIN` shows the unread subquery needs a partial index, `(conversation_id, seq) WHERE deleted_at IS NULL`. It's evaluated here and recorded in the PR.

## API changes
- `PUT /api/v1/conversations/{id}/read-cursor` `{last_read_seq}` → `200 {last_read_seq, unread_count}`; 422 if the value is above `last_message_seq`; 404 for non-members.
- `GET /conversations` now returns real `unread_count` and `my_last_read_seq` values. `unread_mention_count` stays 0 until M10.

## WebSocket changes
- New durable events: `conversation.read` (to the reader's own connections) and `receipt.updated` (to the other members, direct and small groups only), as defined in [08 §15.4](../design/08-websocket.md).

## Tests
- Integration (R-10): out-of-order concurrent PUTs (seq 10, 5, 8) → the final cursor is 10.
- API: the unread count ignores the caller's own messages and deleted messages; it goes to 0 after reading up to `last_message_seq`.
- API: a cursor above `last_message_seq` → 422; a non-member → 404.
- WS: reading in tab 1 → tab 2 receives `conversation.read` with `unread_count: 0`. In a DM, the other member receives `receipt.updated`; in a group of 25 they don't.
- Performance sanity: 50 conversations × 10k messages each → `GET /conversations` completes in under 100 ms on the dev machine (noted, not asserted in CI).
- Frontend unit: the visibility hook sends only when the tab is visible and focused and the conversation is open; debouncing merges rapid scroll updates.

## Acceptance criteria
- User B, viewing conversation Y, sees an unread badge increase on conversation X as A sends messages. Opening X clears the badge in every one of B's tabs.
- In a DM, A sees "Seen" once B has read the message.
- Reloading shows the same unread counts as before (they're server-derived, not client state).
