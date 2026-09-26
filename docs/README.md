# Chat App — Design Package (HLD + LLD)

The architecture and design for a real-time team chat application: FastAPI, PostgreSQL, WebSockets and React. The source requirements are in [../chat_app_requirement.md](../chat_app_requirement.md). This package is the **spec** for the implementation agent. It contains no implementation code.

## Reading order

| # | Document | Requirement sections covered |
|---|---|---|
| 1 | [design/01-scope-and-requirements.md](design/01-scope-and-requirements.md) | §1 Product scope, §2 Functional requirements, §3 Non-functional requirements |
| 2 | [design/02-hld.md](design/02-hld.md) | §4 System HLD |
| 3 | [design/03-backend-architecture.md](design/03-backend-architecture.md) | §5 Backend architecture, §6 Module boundaries, §7 Directory structure |
| 4 | [design/04-domain-model.md](design/04-domain-model.md) | §8 Domain model |
| 5 | [design/05-database.md](design/05-database.md) | §9 ER design, §10 Indexes and constraints |
| 6 | [design/06-auth-and-authorization.md](design/06-auth-and-authorization.md) | §11 Authentication, §12 Authorization |
| 7 | [design/07-rest-api.md](design/07-rest-api.md) | §13 REST API contract |
| 8 | [design/08-websocket.md](design/08-websocket.md) | §14 WebSocket architecture, §15 WebSocket event contracts |
| 9 | [design/09-scheduled-messages.md](design/09-scheduled-messages.md) | §16 Scheduled messages, §17 Background processing |
| 10 | [design/10-errors-logging-security.md](design/10-errors-logging-security.md) | §18 Error handling, §19 Logging/observability, §20 Security |
| 11 | [design/11-testing.md](design/11-testing.md) | §21 Testing |
| 12 | [design/12-local-dev-docker.md](design/12-local-dev-docker.md) | §22 Docker/local development |
| 13 | [design/13-failure-and-concurrency.md](design/13-failure-and-concurrency.md) | §23 Failure scenarios, §24 Concurrency analysis |
| 14 | [design/14-milestones.md](design/14-milestones.md) + [milestones/](milestones/) | §25 Development milestones |
| 15 | [design/15-sonnet-strategy.md](design/15-sonnet-strategy.md) | §26 Sonnet implementation strategy |

Also: [decisions/open-questions.md](decisions/open-questions.md) records the spec questions raised during implementation and how each was resolved, and [briefs/](briefs/) holds the per-milestone implementation briefs for the agent.

## Glossary

| Term | Meaning |
|---|---|
| **seq** | A per-conversation, monotonically increasing message number (1, 2, 3…). It defines message order and is the basis for read cursors and history pagination |
| **Read cursor** | `conversation_members.last_read_seq`: everything at or below it counts as read by that member |
| **Outbox** | The `event_outbox` table. Every durable real-time event is written to it in the same transaction as the state change it describes |
| **Event id** | `event_outbox.id` (bigserial). The client's resume cursor (`last_event_id`) |
| **Durable vs ephemeral event** | Durable events are written to the outbox and can be replayed. Ephemeral events (typing, presence) are sent once and never stored |
| **Session / family** | One login. Every refresh token in a rotation chain shares a `family_id`, which also appears in the access token as `sid` |
| **WS ticket** | A single-use, 30-second credential used to authenticate a WebSocket handshake |
| **UoW** | Unit of Work: the `async with` context manager that owns a DB transaction |
| **Claim** | The worker locking due scheduled rows with `FOR UPDATE SKIP LOCKED` |

## Architecture Decision Records

### ADR-001: No Redis, Celery, Kafka in v1
**Decision:** use PostgreSQL alone for cross-process events (LISTEN/NOTIFY + outbox), the scheduling queue (the `scheduled_messages` table + `SKIP LOCKED`) and persistence.
**Why:** the requirements ask for infrastructure only when it's justified. Each need has a correct Postgres answer at this scale, and a queue would still need a DB-level idempotency guard, so it would add infrastructure without removing the hard part.
**Revisit when:** there are several API instances *and* NOTIFY throughput becomes the bottleneck; the system needs more than about 1k scheduled deliveries per second; or non-Python consumers need the event stream. Seams: the `EventPublisher` Protocol and the `ExecuteScheduledMessage` use case.

### ADR-002: Single API instance first
**Decision:** run one uvicorn process (`--workers 1`) that holds every WebSocket, plus 1..N separate worker processes.
**Why:** presence and fan-out are then simple in-memory structures, with no shared presence store. Worker events still reach the API through NOTIFY.
**Consequences:** rate limits are per process, and presence is reset at API boot. The upgrade path is in [02 §4.5](design/02-hld.md).

### ADR-003: Read cursors instead of per-message read rows
**Decision:** replace the suggested `message_reads` table with `conversation_members.last_read_seq`.
**Why:** marking read becomes one idempotent, monotonic update instead of N inserts, and unread count is an index range count. Per-message read rows grow as members × messages and add nothing that chat needs.
**Trade-off:** there are no per-message "read at" timestamps. Receipts are "read up to seq X", which is the model Slack and WhatsApp group read markers use.

### ADR-004: Transactional outbox + NOTIFY as the only publish path
**Decision:** every durable event is inserted into `event_outbox` inside the business transaction. An `AFTER INSERT` trigger fires NOTIFY with the id, the API's listener re-reads the row and fans it out.
**Why:** Postgres delivers NOTIFY only at commit, so events for rolled-back work are never sent, and the "commit → publish" gap is closed. REST-originated and worker-originated events share one code path, and the table doubles as the replay log for reconnecting clients.

### ADR-005: Outbox replay with overlap window
**Decision:** replay from `after_event_id` using a 15-second `created_at` overlap window, with client dedup and per-conversation seq-gap detection as a backstop. Transaction lengths are bounded by `statement_timeout` and `idle_in_transaction_session_timeout`.
**Why:** bigserial ids are assigned at insert time, not commit time, so plain `id > cursor` replay can skip events that commit late. The `xid8` snapshot-horizon technique is exact but much harder to reason about and test. The overlap approach is simple, bounded and has a correctness backstop.
**Revisit when:** a sync test or production signal shows a missed event.

### ADR-006: WebSocket authentication with single-use tickets
**Decision:** `POST /auth/ws-ticket` (bearer) → a 30 s single-use ticket → `/ws?ticket=`. It is consumed atomically before the socket is treated as authenticated, and the connection is bound to the session (`sid`).
**Why:** browsers can't set headers on a WebSocket, a JWT in the URL leaks into logs, and first-frame authentication leaves unauthenticated sockets open. Tying the socket to the session makes logout close exactly the right sockets.

### ADR-007: Refresh token in an httpOnly cookie, access token in memory
**Decision:** the refresh cookie is `HttpOnly; Secure; SameSite=Strict; Path=/api/v1/auth`. Refresh tokens are opaque, stored hashed, rotated on every use, and reuse is detected per family with a 10 s benign-race grace period.
**Why:** XSS can't read the long-lived credential, and bearer-only APIs aren't CSRF-able. The two cookie endpoints are protected by SameSite, path scoping and a custom header.

### ADR-008: Scheduler without a `processing` state
**Decision:** workers hold claimed rows under a row lock for a short, DB-only delivery transaction, with a savepoint per item.
**Why:** a crash rolls back and releases the lock automatically, so there are no stuck rows to lease-expire. It is exactly-once in effect, backed by `UNIQUE(messages.scheduled_message_id)`.
**Constraint:** delivery must stay DB-only and fast. If external calls (email, push) are ever added, they must go out through the outbox asynchronously, never while the lock is held.

### ADR-009: Server-computed fan-out; no client subscriptions
**Decision:** recipients are computed from membership at write time and stored in the outbox row. Clients never subscribe to conversations.
**Why:** a client can't subscribe to something it's not allowed to see, there is no subscription state to reconcile after a reconnect, and membership in Postgres stays the single source of truth.

### ADR-010: REST for every mutation; WebSocket for delivery and ephemeral signals only
**Decision:** client→server WS frames are limited to `ping`, `sync.request` and `typing.*`.
**Why:** each mutation then has one validation, authorization, idempotency, rate-limit and logging path, and it's easier to test.
**Trade-off:** one HTTP round trip per send, which is negligible for chat.

## Traceability

### Core features (requirements "Core Chat Features" 1–20, "Scheduled Messages")

| Requirement | Design | Milestone |
|---|---|---|
| 1 Register / log in | [06 §11](design/06-auth-and-authorization.md), [07 §Auth](design/07-rest-api.md) | M02 |
| 2 User profile | [07 §Users](design/07-rest-api.md) | M03 |
| 3 Search users | [07 §Users](design/07-rest-api.md), [05 §users](design/05-database.md) (trigram) | M03 |
| 4 One-to-one conversation | [05 §conversations](design/05-database.md) (`direct_key`), [13 R-2](design/13-failure-and-concurrency.md) | M04 |
| 5 Group conversations | [07 §Conversations](design/07-rest-api.md) | M04 |
| 6 / 7 Add / remove members | [07 §Conversations](design/07-rest-api.md), [06 §12](design/06-auth-and-authorization.md), [13 R-12/R-14](design/13-failure-and-concurrency.md) | M04 |
| 8 Send messages | [02 §4.2](design/02-hld.md), [07 §Messages](design/07-rest-api.md), [13 R-1/R-5](design/13-failure-and-concurrency.md) | M05 |
| 9 / 10 Edit / delete own messages | [07 §Messages](design/07-rest-api.md), [13 R-13](design/13-failure-and-concurrency.md) | M05 |
| 11 Reply to messages | [05 §messages](design/05-database.md) (`reply_to_id`) | M05 |
| 12 Message history | [07 §Messages](design/07-rest-api.md) (keyset by seq) | M05 |
| 13 Search messages | [07 §Search](design/07-rest-api.md), [05 §messages](design/05-database.md) (`search_vector`) | M12 |
| 14 Unread counts | [04 §8.4](design/04-domain-model.md), ADR-003 | M07 |
| 15 Mark messages read | [07 §Conversations](design/07-rest-api.md) (read-cursor), [13 R-10](design/13-failure-and-concurrency.md) | M07 |
| 16 Presence | [08 §14.7–14.8](design/08-websocket.md) | M08 |
| 17 Typing indicators | [08 §14.8](design/08-websocket.md) | M08 |
| 18 Real-time messages | [08](design/08-websocket.md), ADR-004 | M06 |
| 19 Notifications | [05 §notifications](design/05-database.md), [07 §Notifications](design/07-rest-api.md) | M10 |
| 20 @mentions | [07 §Messages](design/07-rest-api.md) (mention parsing), [05 §message_mentions](design/05-database.md) | M10 |
| Scheduled: create / view / edit / cancel / deliver | [09](design/09-scheduled-messages.md), [07 §Scheduled messages](design/07-rest-api.md) | M11 |
| Scheduled: races, duplicates, retry, transactions, failures, idempotency, restarts, time zones, UTC, observability | [09 §16.2–17.5](design/09-scheduled-messages.md), [13 R-3/R-4](design/13-failure-and-concurrency.md) | M11 |
| Recurring-ready | [09 §17.7](design/09-scheduled-messages.md) | — (future) |

### Important user scenarios
Every scenario is mapped in [13 §23.1](design/13-failure-and-concurrency.md) and has an E2E test in [M13](milestones/M13-frontend-polish-e2e.md).

### Security bullets
Password hashing, JWT, refresh tokens, revocation, brute force, authorization, WS auth, message authorization, membership validation, input validation, SQL injection, sensitive logging, rate limiting, CORS, secure config and secret management are each a row in [10 §20](design/10-errors-logging-security.md).

### Testing bullets
Unit, integration, API, WebSocket, authentication, authorization, scheduler, concurrency and failure/retry tests are each a subsection of [11 §21.4](design/11-testing.md).

### Logging bullets
Structured logs, levels, request/correlation ids, user context, WS lifecycle, auth/security events, scheduler logs, message logs, stack traces and no secrets are covered in [10 §19](design/10-errors-logging-security.md) and [09 §17.5](design/09-scheduled-messages.md).

## Deviations from the requirement document's suggestions (deliberate)

1. `message_reads` → per-member read cursors (ADR-003).
2. "Conversation subscriptions" → server-computed fan-out (ADR-009).
3. The example event envelope gains `v`, `id` (the resume cursor) and `correlation_id`, and renames `timestamp` → `occurred_at` ([08 §15.1](design/08-websocket.md)).
4. The example path `/api/v1/messages/...` is kept for item operations only. Listing and creating are nested under conversations ([07 §13.2](design/07-rest-api.md)).
5. `activity/audit records` → `audit_logs` (security and admin actions) plus the `event_outbox` (short-lived activity stream). There is no general "activity" table, because nothing reads one.
6. Additions not in the list: `ws_tickets`, `message_mentions`, `event_outbox`.
