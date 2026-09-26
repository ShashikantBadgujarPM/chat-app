# M11 — Scheduled Messages and the Worker Process

## Goal
Make scheduled messages a complete, core feature. Users can schedule, list, view, edit, cancel and retry them, with offset-qualified times stored in UTC. A separate worker process claims due messages with `FOR UPDATE SKIP LOCKED` and delivers each one in a single transaction by calling the shared `MessagingService.create_message`, with a savepoint per item. Failures are classified as transient (retried with backoff) or permanent (marked `failed`, sender notified). The worker also runs the housekeeping jobs (outbox retention, ticket cleanup, token cleanup) under advisory locks, writes a heartbeat, and shuts down gracefully. This is the milestone that tests the system's concurrency guarantees hardest.

## Dependencies
M09, M10.

## Files/modules expected
- `backend/app/modules/scheduling/domain/scheduled_message.py`: dataclass, `ScheduledStatus`, `can_edit`/`can_cancel`/`can_retry`, `backoff(attempts)`, `classify_failure(exc) -> Transient | Permanent`
- `backend/app/modules/scheduling/application/scheduling_service.py` (API side): `create` (idempotent), `list`, `get`, `update`, `cancel`, `retry`, each a single conditional statement as in [09 §16.4](../design/09-scheduled-messages.md); each publishes `scheduled_message.updated`
- `backend/app/modules/scheduling/application/delivery_service.py` (worker side): `deliver(uow, row)` covering re-validation, `create_message(..., scheduled_message_id=row.id)`, marking sent, the outbox event and audit; the failure paths
- `backend/app/modules/scheduling/infrastructure/`: ORM model, repository (`claim_due(batch_size)` using the raw `text()` claim query, `mark_sent`, `mark_retry`, `mark_failed`, conditional updates)
- `backend/app/modules/scheduling/api/`: router + schemas (the `scheduled_at` validator rejects naive datetimes)
- `backend/app/worker.py`: entrypoint; `Worker` class with `run()`, the scheduled loop, `housekeeping_loop(job, interval, advisory_key)`, SIGTERM → `asyncio.Event`, heartbeat file, `on_claimed` test hook
- `backend/app/platform/clock.py`: the `Clock` Protocol, `SystemClock`, and the test `FrozenClock`
- `docker-compose.yml`: the `worker` service (same image, `command: python -m app.worker`, heartbeat healthcheck, `stop_grace_period: 15s`)
- `frontend/src/features/scheduled/`: a "Schedule send" option in the composer (date and time picker in the user's time zone, sending an offset-qualified ISO string), a scheduled-messages panel (tabs by status; edit, cancel and retry actions; live updates through `scheduled_message.*` events), and an inline "scheduled" note on messages that came from a schedule

## Database changes
- `scheduled_messages` as in [05](../design/05-database.md), including `sent_at`, the partial index `(status, next_attempt_at) WHERE status='pending'`, `(sender_id) WHERE status='pending'`, `UNIQUE(client_message_id)` and the nullable `recurrence_rule`.
- On `messages`: add the FK `scheduled_message_id → scheduled_messages(id) ON DELETE SET NULL` and **`UNIQUE(scheduled_message_id)`**. Then add the FK `scheduled_messages.reply_to_id → messages(id)` with `ALTER TABLE`, because the two FKs form a cycle.

## API changes
From [07 §Scheduled messages](../design/07-rest-api.md): `POST /scheduled-messages`, `GET /scheduled-messages`, `GET /scheduled-messages/{id}`, `PATCH /scheduled-messages/{id}`, `POST /scheduled-messages/{id}/cancel`, `POST /scheduled-messages/{id}/retry`.

## WebSocket changes
- New durable events for the sender: `scheduled_message.updated`, `scheduled_message.sent`, `scheduled_message.failed`.
- Delivery also produces the normal `message.created` (all members) and any `notification.created` events through the shared write path, plus a `scheduled_failed` notification for the sender on permanent failure.

## Tests
Everything in the scheduler and concurrency sections of [11 §21.4](../design/11-testing.md), and in particular:
- **N workers × M rows** (4 × 200) → exactly 200 messages, and no `scheduled_message_id` appears twice (R-3).
- **Cancel or edit versus a worker holding the lock** in both orders, using the `on_claimed` hook (R-4): cancel blocks, then gets 409; cancelling first means the message is never delivered.
- **Atomicity:** inject an exception after the message insert → no message, no outbox rows, and the scheduled row goes to retry.
- A savepoint per item: in a batch of 3, the middle item failing doesn't affect the other two.
- Permanent failures: the sender was removed from the group → `failed` + `sender_not_member` + a notification + a `scheduled_message.failed` event; more than 24 h overdue → `failed: expired`.
- Transient failures: `OperationalError` → retry with backoff; 5 attempts → `failed: max_attempts_exceeded`.
- Restart: create due rows while no worker runs, then start a worker → they're delivered.
- Graceful shutdown: SIGTERM mid-batch → the batch commits and the worker exits 0.
- Housekeeping: two workers → each job runs on only one of them (advisory lock, R-17); retention deletes outbox rows older than 7 days only.
- API: a naive datetime → 422; less than 30 s in the future → 422; more than 1 year → 422; an invalid timezone → 422; 101 pending → 429; editing a sent message → 409; cancel is idempotent; retry only works from `failed`.
- Time zones: `2026-10-01T18:00:00+05:30` is stored as `12:30Z`, returned with a `Z`, and `timezone` is kept as sent.

## Acceptance criteria
- The requirement example works end to end: schedule "Hey Rahul, please check the deployment." for 18:00 in the user's time zone. At that time (±2 s) Rahul receives it live, with a mention notification, and the sender's scheduled list shows it as sent.
- Stop the worker, let a message come due, and start the worker again: it's delivered right away. Run `docker compose up --scale worker=3`: no duplicates.
- Cancelling a message a few seconds before it's due works. Cancelling while it's being sent returns a clear "already sent" error in the UI.
- The worker's logs show `scheduler.message_sent` with `lateness_ms`, and `scheduler.stats` every 60 s. Message bodies never appear in the logs.
