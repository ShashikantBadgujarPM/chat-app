# 1–3. Scope, Functional Requirements, Non-Functional Requirements

## 1. Product Scope

A production-style, real-time team chat application (Slack/Teams-lite), built primarily as a **Python backend engineering learning vehicle**. The backend is the graded artifact; the frontend exists to exercise the backend end-to-end and to make the system demoable.

**In scope for v1:**
- Username/password authentication owned by our own backend (no managed IdP).
- 1:1 and group conversations, membership management.
- Message send/edit/delete/reply, history, full-text search.
- Unread counts and read receipts via per-member read cursors.
- Presence (online/offline/last-seen) and typing indicators.
- Real-time delivery over WebSockets, with PostgreSQL as the single source of truth.
- One-time scheduled messages with a crash-safe, exactly-once-effect worker.
- @mentions and a notification feed.
- Structured logging with correlation IDs.

**Explicitly out of scope for v1** (called out because the requirements doc invites challenge):
- File/media attachments (not mentioned in requirements; would add storage, virus-scanning, and CDN concerns — deferred).
- Message reactions/emoji (not requested; trivial to bolt onto the `messages` + event model later).
- Recurring scheduled messages (explicitly deferred by the spec; schema leaves room via nullable `recurrence_rule`).
- Multi-instance horizontal scaling of the API/WebSocket layer (deferred; see [ADR-002](../README.md#adr-002-single-api-instance-first) — architecture documents the upgrade path but does not build it).
- Push notifications to mobile/desktop OS (web notification feed only).
- E2E encryption.
- Admin/moderation tooling beyond group owner add/remove.

## 2. Functional Requirements

Numbered to match the requirement doc's "Core Chat Features" and "Scheduled Messages" lists; each is traced to a design section in [docs/README.md](../README.md#traceability).

| # | Requirement | Primary design section |
|---|---|---|
| FR-1 | Register / log in | [06-auth-and-authorization.md](06-auth-and-authorization.md) |
| FR-2 | Maintain user profile | [07-rest-api.md](07-rest-api.md) §Users |
| FR-3 | Search users | [07-rest-api.md](07-rest-api.md) §Users, [05-database.md](05-database.md) §pg_trgm |
| FR-4 | Start 1:1 conversation | [05-database.md](05-database.md) §conversations, [07-rest-api.md](07-rest-api.md) §Conversations |
| FR-5 | Create group conversation | same |
| FR-6 | Add group members | same |
| FR-7 | Remove group members | same |
| FR-8 | Send messages | [07-rest-api.md](07-rest-api.md) §Messages, [08-websocket.md](08-websocket.md) |
| FR-9 | Edit own message | same |
| FR-10 | Delete own message | same |
| FR-11 | Reply to messages | [05-database.md](05-database.md) §messages.reply_to_id |
| FR-12 | Message history | [07-rest-api.md](07-rest-api.md) §history pagination |
| FR-13 | Search messages | [05-database.md](05-database.md) §tsvector |
| FR-14 | Unread counts | [04-domain-model.md](04-domain-model.md) §ReadCursor |
| FR-15 | Mark messages read | [08-websocket.md](08-websocket.md) §message.read |
| FR-16 | Online/offline presence | [08-websocket.md](08-websocket.md) §presence |
| FR-17 | Typing indicators | [08-websocket.md](08-websocket.md) §typing |
| FR-18 | Real-time message delivery | [08-websocket.md](08-websocket.md) |
| FR-19 | Notifications | [09-scheduled-messages.md](09-scheduled-messages.md) §notifications, [08-websocket.md](08-websocket.md) |
| FR-20 | @mentions | [04-domain-model.md](04-domain-model.md), [05-database.md](05-database.md) §message_mentions |
| FR-21 | Schedule a message | [09-scheduled-messages.md](09-scheduled-messages.md) |
| FR-22 | View scheduled messages | same |
| FR-23 | Edit scheduled messages | same |
| FR-24 | Cancel scheduled messages | same |
| FR-25 | Deliver scheduled messages at due time | same |

## 3. Non-Functional Requirements

| ID | Requirement | Design response |
|---|---|---|
| NFR-1 | PostgreSQL is the single source of truth; WebSockets are delivery-only | Every mutation is REST/worker → DB transaction → commit → outbox event → WS fan-out. See [02-hld.md](02-hld.md). |
| NFR-2 | No premature infra (Redis/Celery/Kafka) unless justified | Cross-process events use Postgres `LISTEN/NOTIFY` + an outbox table; scheduler uses `SELECT … FOR UPDATE SKIP LOCKED`. See [ADR-001](../README.md#adr-001-no-redis-celery-kafka-in-v1). |
| NFR-3 | Correctness under concurrency (races, dup scheduler execution, multi-tab) | [13-failure-and-concurrency.md](13-failure-and-concurrency.md). |
| NFR-4 | Security: hashing, JWT, revocation, CSRF, injection, rate limiting | [10-errors-logging-security.md](10-errors-logging-security.md). |
| NFR-5 | Observability: structured logs, correlation IDs, no secrets logged | same. |
| NFR-6 | Testability: unit/integration/API/WS/concurrency tests | [11-testing.md](11-testing.md). |
| NFR-7 | Local reproducibility via Docker Compose | [12-local-dev-docker.md](12-local-dev-docker.md). |
| NFR-8 | Clean/layered architecture, approachable to a Node/Express developer | [03-backend-architecture.md](03-backend-architecture.md). |
| NFR-9 | Genuine (non-contrived) use of Python language features | [03-backend-architecture.md](03-backend-architecture.md) §Python feature map. |
| NFR-10 | Message history remains fast as volume grows | Per-conversation `seq` + keyset pagination + covering index; see [05-database.md](05-database.md). |
| NFR-11 | Scheduled messages survive process restarts, never double-send | [09-scheduled-messages.md](09-scheduled-messages.md). |

## Assumptions challenged / decisions made

1. **`message_reads` as a per-message-per-user row does not scale** and makes "unread count" an expensive `COUNT` over a growing join table. Replaced with per-member **read cursors** (a single `last_read_seq` column per membership) — O(1) to update, O(1) to compute unread count via an index range scan. See [ADR-003](../README.md#adr-003-read-cursors-instead-of-per-message-read-rows).
2. **Typing indicators must not be persisted** (explicit in the spec) — they never touch the outbox/event table; they are transient, best-effort, in-memory-routed events only.
3. **"WebSocket subscriptions to conversations"** is replaced by **server-computed fan-out from conversation membership**. A client does not need to explicitly subscribe/unsubscribe per conversation; this removes a whole class of bugs (forgetting to subscribe, subscribing to conversations you're not a member of) and matches "PostgreSQL is the source of truth."
4. **A queue framework is not needed for scheduled messages at this scale.** `FOR UPDATE SKIP LOCKED` on a `scheduled_messages` table gives safe concurrent claiming with plain PostgreSQL, which satisfies "do not prematurely introduce a distributed task queue." The design documents the exact seam (`SchedulerPort`) where Celery/RQ could later replace the poll loop.
5. **WebSocket authentication cannot use the JWT directly as a query parameter** (it would land in server access logs and browser history). A short-lived, single-use **WS ticket** issued over authenticated REST is used instead.
