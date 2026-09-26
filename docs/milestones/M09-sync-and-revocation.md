# M09 — Reconnect Sync and Session Revocation

## Goal
Make reconnection correct. A reconnecting client replays the durable events it missed, from its `last_event_id`, using the 15-second overlap window. Live events that arrive during the replay are buffered and deduplicated. Old or huge gaps trigger `sync.reset_required`. The listener broadcasts `sync.required` after its own reconnect. The client backstops message history with per-conversation seq-gap detection. WS connections are bound to a login session, and `session.revoked` closes that session's sockets immediately on logout, logout-all, password change, reuse detection or account disable.

## Dependencies
M06, M07.

## Files/modules expected
- `backend/app/realtime/sync_service.py`: `replay(user_id, after_event_id)`, an **async generator** yielding batches of up to 200 events that uses the overlap-window query from [08 §14.5](../design/08-websocket.md); decides when a reset is needed (the cursor has been pruned, or more than 5,000 events would be replayed)
- `backend/app/realtime/connection_manager.py`: the `SYNCING` → `LIVE` connection state, `sync_buffer`, flush with dedup, `_by_session`, `close_session(family_id | "*")`, and a 5-minute session revalidation task (one batched query)
- `backend/app/realtime/outbox_listener.py`: on reconnect, broadcasts `sync.required` to every connection; handles internal `session.revoked` events (closes the matching sockets with 4001 and doesn't forward the event)
- `backend/app/realtime/gateway.py`: handles the `sync.request` frame
- `backend/app/modules/identity/application/`: `Logout`, `LogoutAll`, `RevokeSession`, `ChangePassword`, the reuse-detection path and account deletion now **publish `session.revoked`** in the same UoW
- `backend/app/api/sync_router.py`: `GET /api/v1/sync/events?after_event_id=&limit=` (a REST fallback with the same semantics)
- `frontend/src/ws/sync.ts`: persists `last_event_id` (in memory plus `sessionStorage` for a tab reload, guarded by try/catch); sends `sync.request` after `hello`; applies the batches; keeps a dedup set of the last 2,000 ids; on `sync.reset_required` invalidates every React Query cache and refetches; on `sync.required` re-syncs
- `frontend/src/features/messages/seqGap.ts`: if an incoming `message.created` seq is greater than the known max seq + 1, fetches `after_seq` for that conversation

## Database changes
None beyond M06. Confirm that `idle_in_transaction_session_timeout = '10s'` is set on the app role and `statement_timeout = '5s'` on outbox-writing transactions, since the overlap window depends on them. Add a test that asserts both are set.

## API changes
- `GET /api/v1/sync/events` (bearer) → `{events, has_more, reset_required}`.

## WebSocket changes
- New client frame: `sync.request {after_event_id|null}`.
- New server frames: `sync.batch`, `sync.complete`, `sync.reset_required`, `sync.required`.
- The internal event `session.revoked` closes sockets with **4001 `session_revoked`**.

## Tests
- WS: disconnect, create 30 events over REST, reconnect and sync → exactly those 30 in id order (plus possibly some overlap events the client dedups), then `sync.complete`.
- WS (R-16): events created *during* a slow replay (a test hook pauses the generator) are delivered exactly once after `sync.complete`.
- WS (R-9, **out-of-order commit**): T1 inserts outbox id N and stays open; T2 inserts and commits N+1; the client receives N+1 and disconnects; T1 commits; the client syncs from N+1 → it receives N.
- WS: a pruned cursor → `sync.reset_required`; an `after_event_id` of `null` → an immediate `sync.complete`.
- WS: killing the listener's backend (`pg_terminate_backend`) → every client receives `sync.required` after the listener reconnects.
- WS: logout in session S → S's two tabs close with 4001 `session_revoked`, and session S2's socket stays open; logout-all → every socket closes.
- API: `/sync/events` matches the WS replay.
- Frontend unit: the dedup set, the seq-gap detection path, and reset handling.

## Acceptance criteria
- Toggle the network off in DevTools for 30 s while another user sends 10 messages, then turn it back on. Every message appears in order without a page reload, with no duplicates.
- Restart the `db` container. After recovery, clients resync automatically, and messages sent during the listener outage show up.
- Logging out in one browser disconnects that browser's other tabs within 1 s, and another browser logged in as the same user stays connected.
