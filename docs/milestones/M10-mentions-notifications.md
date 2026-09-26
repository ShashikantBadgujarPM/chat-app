# M10 — @Mentions and Notifications

## Goal
Parse `@username` mentions on the server, resolving them against the conversation's active members, and store them. The conversation list gets an unread mention count. A persisted notification feed covers mentions, being added to or removed from a group, and (from M11) failed scheduled messages. Notifications are delivered in real time with `notification.created`. Editing a message re-notifies only newly added mentions, and deleting a message removes its mentions.

## Dependencies
M05, M06.

## Files/modules expected
- `backend/app/modules/messaging/domain/mentions.py`: `extract_mention_handles(body) -> Iterator[str]`, a **generator** using the regex `(?<![\w@])@([A-Za-z0-9_]{3,32})`, deduplicated case-insensitively, capped at 50
- `backend/app/modules/messaging/application/messaging_service.py`: `create_message` and `edit_message` resolve handles against the active members (one query), write `message_mentions`, compute *added* mentions on edit (a set difference), and call the notification service; `delete_message` deletes mentions
- `backend/app/modules/notifications/`: domain `Notification`, `NotificationType`; application `NotificationService.create(uow, user_id, type, conversation_id, message_id, payload)` (honors `notifications_muted` for mentions), which writes the row and the `notification.created` outbox event; repository; router
- The conversation service: `add_members` and `remove_member` create `group_added` and `group_removed` notifications
- `frontend`: mention autocomplete in the composer (typing `@` searches the conversation's members); mentions rendered as highlighted chips; a notification bell with an unread badge and a dropdown feed; a toast when a mention arrives for a conversation that isn't open; an `@` badge on conversations with unread mentions

## Database changes
- `message_mentions` (composite PK, `INDEX(mentioned_user_id)`).
- `notifications` (with the `(user_id, created_at DESC)` index and the partial unread index), per [05](../design/05-database.md). The `type` CHECK constraint includes `scheduled_failed` now, so M11 doesn't need another migration.

## API changes
- `Message.mentions` is now filled (a list of user ids).
- `ConversationSummary.unread_mention_count` is real: a count over `message_mentions` joined to `messages`, filtered to `seq > last_read_seq`.
- `GET /api/v1/notifications`, `POST /api/v1/notifications/{id}/read`, `POST /api/v1/notifications/read-all`.

## WebSocket changes
- New durable event: `notification.created` (recipient: the notified user).

## Tests
- Unit: mention extraction edge cases: `email@x.com` isn't a mention; `@@user`, trailing punctuation (`@rahul,`), duplicates (`@Rahul @rahul` gives one), the 50 cap, and Unicode text around mentions.
- API: mentioning a non-member → no mention and no notification; mentioning yourself → no notification; mentioning in a muted conversation → a mention row but no notification.
- API: editing to add `@b` → B is notified once; editing again without changes → no new notification; editing to remove `@b` → the mention row is removed and the old notification is kept (history).
- API: delete → mentions removed, and `unread_mention_count` goes down.
- API: being added to a group → a `group_added` notification; being removed → `group_removed`.
- WS: B, viewing another conversation, receives `notification.created` and `message.created` when A mentions B.
- API: notification pagination; read and read-all are idempotent; another user's notification id → 404.

## Acceptance criteria
- Typing `@ra` in a group suggests Rahul; sending gives Rahul a toast and a bell badge, even while he's in another conversation.
- The conversation shows an `@` badge until it's read.
- Being added to a group produces a notification that links to the group.
