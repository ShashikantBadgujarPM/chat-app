# 23–24. Failure Scenarios and Concurrency / Race-Condition Analysis

## 23. Failure scenarios

### 23.1 Requirement scenarios → handling

Every "Important User Scenario" from the requirements doc, traced to its mechanism.

| Scenario | Handling | Where |
|---|---|---|
| Normal message (both online) | REST → transaction (message + outbox) → commit → NOTIFY → listener → fan-out to every member connection | [02](02-hld.md) §4.2 |
| Offline receiver | Nothing to push. On return, `GET /conversations` unread counts come from the cursors; history comes from REST | [02](02-hld.md) §4.3 |
| Receiver in another conversation | `message.created` still arrives; the client bumps that conversation's badge; a mention also gives `notification.created` | [08](08-websocket.md) §15.5 |
| Group chat | One outbox row, N recipients, one fan-out loop | [08](08-websocket.md) §15.5 |
| Typing indicator | Ephemeral WS frame, membership-checked and throttled, never persisted, expires on the client after 6 s | [08](08-websocket.md) §14.8 |
| Presence and last seen | Connection count 0↔1 transitions, a 10 s disconnect grace period, `user_presence.last_seen_at` persisted, REST snapshot + WS deltas | [08](08-websocket.md) §14.7–14.8 |
| Read receipts | `PUT read-cursor` (monotonic) → `conversation.read` to the reader's own tabs, `receipt.updated` to others | [07](07-rest-api.md) §Conversations |
| Message edit | `PATCH /messages/{id}` → `message.updated` carrying the full message | [07](07-rest-api.md) |
| Message delete | Soft delete (tombstone) → `message.deleted` | [07](07-rest-api.md) |
| WS disconnect | Backoff reconnect → new ticket → `sync.request {after_event_id}` → replay with the overlap window, or a reset; seq-gap detection as a backstop | [08](08-websocket.md) §14.5 |
| Multiple tabs | One `Connection` per tab under the same user; every durable event goes to every tab; client dedup by id; presence counts connections; single-flight token refresh | [08](08-websocket.md) §14.3 |
| Scheduled message | Stored as `pending`, claimed by the worker when due, delivered in one transaction | [09](09-scheduled-messages.md) |
| Scheduled cancellation | Conditional `UPDATE … WHERE status='pending'`; blocks on and then loses to an in-flight delivery → 409 | [09](09-scheduled-messages.md) §16.4 |
| Application restart | No in-memory schedule state. The worker claims whatever is due after it starts; the API resets presence at boot; clients reconnect and sync | [09](09-scheduled-messages.md) §17.4 |
| Duplicate scheduler execution | `FOR UPDATE SKIP LOCKED` (primary guard), `WHERE status='pending'` (state guard), `UNIQUE(messages.scheduled_message_id)` (last-resort constraint) | §24 R-3 |
| Invalid or expired auth | REST: 401 + `WWW-Authenticate` → client refreshes once, then goes to login. WS: close 4001 → refresh + new ticket, or login if `session_revoked` | [06](06-auth-and-authorization.md), [08](08-websocket.md) §14.7 |

### 23.2 Infrastructure failures

| Failure | Effect | Mitigation / recovery |
|---|---|---|
| **Postgres unavailable** | REST → 503/500; worker loop errors | The pool reconnects; `/health/ready` returns 503 so orchestration stops routing; the worker logs and backs off 1→30 s; no partial writes, because every transaction is atomic |
| **LISTEN connection drops** (DB restart, network) | NOTIFYs in that window are lost; live delivery stalls | The listener reconnects with backoff, then broadcasts `sync.required` to all sockets → each client replays from its cursor. Nothing is lost, because the events are in the outbox table |
| **API process crash** | All sockets drop; presence rows go stale | Clients reconnect with backoff and sync. API startup resets every `user_presence` row to offline, and reconnecting users flip themselves back to online |
| **Worker crash mid-batch** | The batch transaction rolls back | Rows stay `pending` and are reclaimed. At-most-once effect comes from the transaction, at-least-once attempt from the next poll |
| **Worker down for a long time** | Backlog builds up | On return it drains in batches. Items overdue past `SCHEDULE_MAX_LATENESS` → `failed: expired` plus a notification. `scheduler.stats` lag shows the outage in the logs |
| **Slow or stalled client** | Its send queue fills | Close 4008 → reconnect and sync. Other clients aren't affected |
| **Client clock wrong** | Would affect "schedule for 6 PM" | The client sends an offset-qualified time chosen by the user; the server validates "in the future" against its own clock and returns the server's interpretation. Display uses `server_time` from `hello` to estimate the skew for relative times ("in 5 min") |
| **Server clock skew between API and worker** | Could cause early or late sends | Every due-time comparison uses DB `now()`, which is one clock |
| **Access token expires during a WS session** | None | WS is session-bound, not token-bound (see [06](06-auth-and-authorization.md) §11.6) |
| **Outbox growth** | The table grows with every event | 7-day retention pruning in the worker (under an advisory lock). Events past retention → `sync.reset_required` |
| **Long-running transaction** | Would widen the out-of-order commit window beyond replay coverage | `statement_timeout` 5 s on outbox-writing transactions, and `idle_in_transaction_session_timeout` 10 s for the role |
| **Deploy while messages are in flight** | REST requests are cut off | uvicorn graceful shutdown finishes in-flight requests. Clients retry mutations with the same `client_message_id`, so replays are idempotent |
| **Poison scheduled message** | Repeats failing | Attempts cap → `failed`. The limitation (a process crash that isn't an exception doesn't increase `attempts`) is accepted and documented in [09](09-scheduled-messages.md) §17.4 |

## 24. Concurrency / race-condition analysis

Default isolation is **READ COMMITTED** (the Postgres default). Each race is closed with the narrowest tool that works: a single conditional statement, then a unique constraint, then a row lock, then an advisory lock. Nothing uses SERIALIZABLE, which would need retry loops everywhere and would make the code harder to follow.

| ID | Race | Guard | Why it works |
|---|---|---|---|
| **R-1** | Two sends to the same conversation at the same instant | `UPDATE conversations SET last_message_seq = last_message_seq+1 … RETURNING` in the send transaction; `UNIQUE(conversation_id, seq)` | The UPDATE takes a row lock on the conversation, so the second sender waits for the first to commit and then increments from the committed value. Sends to *other* conversations don't contend. The lock is held only for a short insert transaction |
| **R-2** | Two users (or two tabs) start the same DM at the same time | `UNIQUE(direct_key)` with `INSERT … ON CONFLICT (direct_key) DO NOTHING RETURNING id`, then `SELECT` by `direct_key` if nothing was returned | One insert wins; the other gets no row back and reads the winner. Members are inserted in the same transaction as the winner's conversation insert |
| **R-3** | Two workers claim the same due scheduled message | `FOR UPDATE SKIP LOCKED` in the claim query | The second worker skips rows the first has locked. If a claim somehow repeated after a commit, `status='pending'` in the claim `WHERE` wouldn't match, and `UNIQUE(messages.scheduled_message_id)` is a last-resort guard in case of a logic bug |
| **R-4** | User cancels or edits while the worker is delivering | Conditional `UPDATE … WHERE status='pending'` | It blocks on the worker's row lock. After the worker commits, READ COMMITTED re-evaluates the WHERE clause (EvalPlanQual), 0 rows match, and the user gets 409. If the user goes first, the worker's claim doesn't match (cancelled), or the row isn't due yet (edited time) |
| **R-5** | Client retries a send (timeout, double click, reconnect) | `UNIQUE(sender_id, client_message_id)` with `ON CONFLICT DO NOTHING` → return the existing row with 200 | Idempotent at the constraint level, so it doesn't depend on timing |
| **R-6** | Two tabs refresh at the same time with the same cookie | `SELECT … FOR UPDATE` on the token row, plus a 10 s grace period for the `replaced_by` case → 409 `refresh_superseded`; single-flight lock on the client | The first rotates the token; the second sees a recently rotated token and gets 409 instead of triggering family revocation, then retries with the new cookie from the shared cookie jar |
| **R-7** | A leaked WS ticket is replayed | `UPDATE ws_tickets SET consumed_at=now() WHERE hash=… AND consumed_at IS NULL AND expires_at>now() RETURNING` | A single atomic statement means only one consumer ever gets a row back |
| **R-8** | Connect and disconnect of the same user interleave (tab reload) | Per-user `asyncio.Lock` around presence transitions, plus the 10 s offline grace timer (cancelled on reconnect) | Transitions are serialized, so the persisted state follows the real connection count, and reloads don't show as offline |
| **R-9** | Outbox ids committing out of order, so cursor-based replay skips an event | Transaction time bounds, a 15 s overlap window on replay, client dedup, seq-gap detection | See [08](08-websocket.md) §14.5. Correctness backstop: message history is re-fetched by `seq` |
| **R-10** | Stale tab sends an older read cursor after a newer one | `SET last_read_seq = GREATEST(last_read_seq, :v)` | Monotonic; order doesn't matter |
| **R-11** | Member removed while a message to that conversation is being sent | Recipients are computed inside the send transaction and frozen into `event_outbox.recipient_user_ids` | Whichever transaction commits first defines the outcome. The removed user gets either the message and then `member_removed`, or only `member_removed`. They can't fetch it afterwards over REST (membership check), and the client drops the conversation on `member_removed` |
| **R-12** | Two owners remove each other (or themselves) at the same time, leaving the group without an owner | `SELECT user_id FROM conversation_members WHERE conversation_id=:c AND role='owner' AND left_at IS NULL FOR UPDATE` before removing an owner; reject if it's the last one | Both transactions lock the same owner rows, so they run one after the other; the second sees one owner left and gets 409 |
| **R-13** | Edit and delete of the same message race | Edit: `UPDATE messages … WHERE id=:id AND sender_id=:me AND deleted_at IS NULL`; delete sets `deleted_at` | If the delete commits first, the edit matches 0 rows → 409 `message_deleted`. If the edit commits first, the delete tombstones the edited version. Both orders end in a consistent state |
| **R-14** | Concurrent add of the same member | `INSERT … ON CONFLICT (conversation_id, user_id) DO UPDATE SET left_at = NULL, joined_at = now() WHERE conversation_members.left_at IS NOT NULL` | Idempotent; also handles re-adding a former member (their `last_read_seq` is set to the current `last_message_seq`, so a returning member doesn't get a huge unread count) |
| **R-15** | Typing membership cache is stale after removal | The cache entry is invalidated when the listener processes the `member_removed` event; 60 s TTL as a safety net | The worst case is a removed user's typing indicator showing briefly for a group they just left. That is accepted: it is ephemeral and never reveals content |
| **R-16** | Live events arrive while a connection's sync replay is still running | Connection state `SYNCING` buffers live events; flush with dedup after `sync.complete` | Nothing is lost in the gap between the replay query and going live |
| **R-17** | Housekeeping jobs running on N workers | `pg_try_advisory_lock(job_key)`, held for the job's session | Only one worker runs each housekeeping job at a time; the others skip it |
| **R-18** | Account lockout counter under a concurrent brute-force burst | `UPDATE users SET failed_login_attempts = failed_login_attempts + 1, locked_until = CASE WHEN failed_login_attempts + 1 >= 5 THEN now() + interval '15 min' ELSE locked_until END WHERE id = :id RETURNING …` | An atomic increment, not read-modify-write in Python, so parallel attempts can't lose updates and get around the lockout |

### 24.1 Asyncio-level (in-process) concurrency notes

- **Connection manager dicts** are mutated without awaits between reading and writing, which is safe on a single event loop. Code review rule: no `await` inside a critical section on these dicts.
- **Fan-out never awaits a socket.** `put_nowait` onto bounded queues decouples producers from slow consumers.
- **Blocking calls** (Argon2 hash/verify) go through `asyncio.to_thread`; everything else is async I/O. `asyncio` debug mode in dev flags anything that blocks the loop for more than 100 ms.
- **Background tasks** (listener, presence timers, periodic revalidation) are created from the lifespan and kept in a `set` with done-callbacks that log exceptions. A task that dies silently is a classic asyncio bug, so every task gets a wrapper that logs unexpected exceptions and restarts the task if it is critical, such as the listener.
