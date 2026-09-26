# M06 — Realtime Core: Outbox, LISTEN/NOTIFY, WebSocket Gateway

## Goal
Deliver committed events to connected clients in real time. This milestone builds the transactional outbox with a NOTIFY trigger, the Postgres outbox publisher, the asyncpg LISTEN loop, WS ticket issuance and consumption, the `/ws` gateway with the `hello`/`ping`/`pong` protocol, and the connection manager with per-connection bounded send queues and multi-tab fan-out. Messages and conversation changes then show up live in the UI. Sync, presence and typing come in later milestones; this one gets the delivery path itself correct.

## Dependencies
M05.

## Files/modules expected
- `backend/app/realtime/envelope.py`: the `WSEvent` dataclass and envelope serialization (the `v`, `id`, `type`, `conversation_id`, `occurred_at`, `correlation_id` and `payload` fields from [08 §15.1](../design/08-websocket.md))
- `backend/app/realtime/publisher.py`: the `EventPublisher` Protocol + `OutboxEventPublisher` (inserts into `event_outbox` in the caller's UoW, taking `correlation_id` from `LogContext`); the M04 no-op stays for unit tests
- `backend/app/realtime/outbox_listener.py`: a dedicated asyncpg connection running `LISTEN events`, micro-batching ids (20 ms / 100 ids), fetching rows, calling `ConnectionManager.deliver`, and reconnecting with backoff (the `sync.required` broadcast on reconnect comes in M09, but the hook exists now)
- `backend/app/realtime/connection_manager.py`: `register`, `unregister`, `deliver(user_id, event)`, `_by_user`, the `Connection` class with `send_queue` (maxsize 256) and `sender_task`, closing with 4008 on overflow, a per-connection frame rate limiter
- `backend/app/realtime/gateway.py`: `/ws` route that checks Origin, consumes the ticket atomically, accepts and closes with 4001 on failure, sends `hello`, runs the receive loop (JSON parse, 16 KB limit, `ping` → `pong`, unknown type → `error`, 10 invalid frames per minute → close 4000), and handles idle timeout
- `backend/app/modules/identity/application/ws_ticket_service.py` + `POST /api/v1/auth/ws-ticket`
- `backend/app/main.py` lifespan: create the `ConnectionManager`, start the listener task (supervised: exceptions are logged and the task restarted), close sockets with 1001 on shutdown
- `backend/app/platform/tasks.py`: `spawn_supervised(coro_factory, name)`, which keeps references to tasks and logs or restarts them if they die
- `frontend/src/ws/`: a `ChatSocket` class that gets a ticket, connects, handles `hello`, sends heartbeat pings (25 s interval, 10 s pong timeout), reconnects with full-jitter backoff, handles close codes per [08 §14.7](../design/08-websocket.md), and dispatches events to the store with dedup by `id`
- The frontend store applies `message.created`/`updated`/`deleted` and the `conversation.*` events, and reconciles optimistic sends by `client_message_id`

## Database changes
- `event_outbox` (bigserial id, `recipient_user_ids uuid[]` with a GIN index, `created_at` index) and an `AFTER INSERT` trigger function `notify_event_outbox()` that runs `pg_notify('events', NEW.id::text)`.
- `ws_tickets` (including `session_family_id`).
- Every outbox-writing transaction sets `SET LOCAL statement_timeout = '5s'` (done in `OutboxEventPublisher` or the UoW factory).

## API changes
- `POST /api/v1/auth/ws-ticket` → `201 {ticket, expires_in: 30}`, rate limited to 10/min per user.
- `/health/ready` also reports the listener's connection status (503 if it's down).

## WebSocket changes
- New endpoint `GET /ws?ticket=…`.
- Server frames: `hello`, `pong`, `ack`, `error`, and the durable events `message.created`, `message.updated`, `message.deleted`, `conversation.created`, `conversation.updated`, `conversation.member_added`, `conversation.member_removed`.
- Client frames: `ping` (the others come in M08 and M09).
- Close codes: 1000, 1001, 1011, 4000, 4001, 4008, 4029.

## Tests
- Integration: an outbox insert followed by rollback → no NOTIFY is received; after commit → a NOTIFY carrying the id is received (`real_commits`, with a separate asyncpg listener in the test).
- WS: a valid ticket → `hello`; invalid, expired or reused tickets → close 4001; a bad Origin → rejected; a race of 2 connects on one ticket → one succeeds (R-7).
- WS: A sends over REST → B's two connections and A's second connection receive `message.created` with the documented envelope, and a non-member's connection receives nothing.
- WS: `member_removed` reaches the removed user; after that, a message sent to the group does not reach them.
- WS: a slow consumer (a real uvicorn server, a client that never reads) → close 4008, while a second client keeps receiving.
- WS: malformed JSON, an unknown type or an oversized frame → `error`; a flood → 4000; an idle socket → closed after 60 s (with the idle timeout configured to 1 s in the test).
- WS: `ping` → `pong`, which includes `server_time`.
- Unit: the connection manager's register, unregister and deliver with fake sockets; queue overflow → close.
- Unit (frontend): the `ChatSocket` reconnect backoff sequence and the close-code → action mapping; the store deduplicates by event id and reconciles optimistic messages.

## Acceptance criteria
- Two browsers logged in as different users see each other's messages, edits and deletes within about 100 ms, without refreshing.
- The same user in two tabs sees the message they sent in one tab appear in the other.
- Killing and restarting the `api` container makes the clients reconnect automatically with backoff; live messages work again afterwards (the missed-event backfill arrives in M09).
- No JWT or ticket appears in any log line; the access log records `/ws` with the query string stripped.
