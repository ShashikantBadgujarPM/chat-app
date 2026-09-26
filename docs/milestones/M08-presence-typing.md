# M08 — Presence and Typing Indicators

## Goal
Add online/offline presence with last-seen times, based on connection counts per user, with a 10-second disconnect grace period and presence persisted for restarts and REST reads. Add typing indicators as ephemeral, membership-checked, throttled WS signals that never touch the database. Presence changes go only to users who share a conversation with the subject.

## Dependencies
M06.

## Files/modules expected
- `backend/app/modules/presence/application/presence_service.py`: `on_connection_opened(user_id)`, `on_connection_closed(user_id)` (with the grace timer), `co_member_ids(user_id)` (cached for 60 s), `get_presence(ids)`
- `backend/app/modules/presence/infrastructure/presence_repository.py`: `upsert_status`, `reset_all_offline()` (called at API startup), `get_many`
- `backend/app/modules/presence/application/typing_service.py`: membership check through the connection manager's TTL membership cache, a throttle of one broadcast per (user, conversation) per 2 s
- `backend/app/realtime/connection_manager.py`: per-user `asyncio.Lock` for transitions, 0↔1 hooks, `deliver_ephemeral(user_ids, envelope)` (no outbox), the membership cache and its invalidation hook
- `backend/app/realtime/gateway.py`: handling of `typing.start`/`typing.stop`
- `backend/app/modules/identity/api/users_router.py`: `GET /users/presence?ids=`; `UserPublic.presence` filled from `user_presence`
- `frontend`: presence dots and "last seen 5 min ago" (relative time, corrected for server skew using `hello.server_time`); a typing indicator line ("Rahul is typing…", "3 people are typing…") with 6 s client-side expiry; the composer sends `typing.start` at most every 3 s while typing and `typing.stop` on send, blur or an empty input

## Database changes
- The `user_presence` table (PK `user_id`, `status`, `last_seen_at`, `updated_at`) from [05](../design/05-database.md). A migration backfills a row per existing user with status `offline`.

## API changes
- `GET /api/v1/users/presence?ids=a,b,c` (up to 100 ids).
- `UserPublic.presence` now holds real data.

## WebSocket changes
- New client frames: `typing.start {conversation_id}`, `typing.stop {conversation_id}`.
- New ephemeral server frames (`id: null`): `presence.updated {user_id, status, last_seen_at}`, `typing.updated {conversation_id, user_id, is_typing, expires_in_s}`.

## Tests
- WS: a user connects → co-members receive `presence.updated online`; a user who shares no conversation with them receives nothing.
- WS: multi-tab: with 3 connections, closing 2 sends nothing and closing the last sends `offline` after the grace period (grace configured to 0.2 s in the test); reconnecting within the grace period sends nothing (R-8).
- WS: typing reaches the other members and isn't echoed to the sender's connections; a non-member → `error`; 10 `typing.start` frames in 1 s → one broadcast (the throttle); `event_outbox` and every table's row counts are unchanged (nothing persisted).
- Integration: API startup marks every presence row offline.
- API: `GET /users/presence` reflects connections, and `last_seen_at` is updated on the final disconnect.
- Unit: the throttle and grace-timer logic with a fake clock and an injected timer.

## Acceptance criteria
- Opening the app in one browser turns the user's dot green for their contacts within about 1 s; closing every tab turns it grey after about 10 s and shows "last seen just now".
- Reloading a tab doesn't flicker presence for other users.
- Typing in a conversation shows the indicator to the other members, and it disappears within 6 s after typing stops, even if the tab is killed.
