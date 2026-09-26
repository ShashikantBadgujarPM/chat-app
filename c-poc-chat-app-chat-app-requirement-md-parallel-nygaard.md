# Plan: HLD/LLD design package for the real-time chat app

## Context
`chat_app_requirement.md` asks for a full architecture and design package: 26 numbered sections, plus milestones that a Claude Sonnet coding agent will implement later. It also asks for the assumptions to be challenged. The repo is greenfield, with only the requirements file. **No implementation code, no FastAPI files, no boilerplate.** The output is markdown design documents only.

You chose:
- Split markdown under `docs/`
- 1 API instance + 1..N scheduler workers, no Redis
- Refresh token in an httpOnly cookie, access token in memory

## Deliverable: files to create

```
docs/
  README.md                         index, reading order, glossary, decision log (ADR list)
  design/
    01-scope-and-requirements.md    §1 scope, §2 FR, §3 NFR, explicit non-goals
    02-hld.md                       §4 HLD (component + sequence diagrams in Mermaid)
    03-backend-architecture.md      §5 layers, §6 module boundaries, §7 directory tree, DI approach
    04-domain-model.md              §8 entities, value objects, invariants, exception hierarchy
    05-database.md                  §9 ER (Mermaid), §10 every table: cols/types/PK/FK/idx/uniq/null/cascade/soft-delete
    06-auth-and-authorization.md    §11, §12 (token lifecycle, rotation, reuse detection, policy matrix)
    07-rest-api.md                  §13 full contract per endpoint + error envelope + pagination
    08-websocket.md                 §14 architecture, §15 event contracts (envelope, every event, close codes)
    09-scheduled-messages.md        §16, §17 state machine, worker loop, locking, retry, recurrence-ready model
    10-errors-logging-security.md   §18, §19, §20
    11-testing.md                   §21 test pyramid, fixtures, concurrency test techniques
    12-local-dev-docker.md          §22 compose services, config/env, migrations flow
    13-failure-and-concurrency.md   §23, §24 scenario tables (each user scenario from the spec → handling)
    14-milestones.md                §25 overview + dependency graph
    15-sonnet-strategy.md           §26 how to drive the Sonnet agent: per-milestone prompts, guardrails, definition of done
  milestones/
    M00-foundation.md … M12-frontend-polish.md   each: Goal, Dependencies, Files/modules, DB changes, API changes, WS changes, Tests, Acceptance criteria
```

## Key architectural decisions (these will be argued in the docs as ADRs)

1. **Cross-process event bus = PostgreSQL LISTEN/NOTIFY + transactional outbox (`events` table).** The worker process has to push WS events into the API process, so some bus is needed no matter what. Postgres already provides one, which justifies leaving Redis out. `pg_notify` issued inside the business transaction is delivered only on commit, which gives exactly the "commit → publish" guarantee with no gap. NOTIFY carries only the event id, because payloads are limited to about 8 KB. The `EventPublisher` interface keeps a later swap to Redis pub/sub possible.
2. **Missed-event sync:** every durable event gets a global `bigserial` id. The client keeps `last_event_id`. On reconnect it sends `{type:"sync", after_event_id}`. The server replays the events the user is authorized for, within a retention window of about 7 days. If the id is too old, it returns `sync.reset_required` and the client refetches through REST. Typing and presence are ephemeral: they are never written to the outbox and are sent with a direct NOTIFY or in-process only.
3. **Replace `message_reads` per-message rows with a per-member read cursor.** Each conversation keeps a `conversations.last_message_seq`. Each message gets a per-conversation `seq`, assigned with `UPDATE conversations SET last_message_seq = last_message_seq + 1 … RETURNING`, which serializes sends within one conversation. Each member keeps `conversation_members.last_read_seq`. Unread count is a count of rows with `seq > last_read_seq`, excluding the user's own and deleted messages, served by the index `(conversation_id, seq)`. Read receipts come from the cursors. This challenges the spec's `message_reads` table.
4. **Idempotent send:** clients send a `client_message_id` (UUID), with `UNIQUE(sender_id, client_message_id)`. Scheduled sends use `messages.scheduled_message_id UNIQUE`, which is the hard guard against double delivery.
5. **Scheduler:** a separate `worker` entrypoint from the same codebase. It polls about every 2 s, plus a NOTIFY wake-up when a message is scheduled soon. It claims rows with `SELECT … WHERE status='pending' AND next_attempt_at<=now() ORDER BY … FOR UPDATE SKIP LOCKED LIMIT n`. Each item runs in one transaction: re-validate status and membership, insert the message, create mention notifications, set status to `sent`, write the outbox event, commit. States: `pending → sent | failed | cancelled`, with `attempts`, `last_error`, and exponential backoff through `next_attempt_at`. Transient errors are retried and permanent ones are not; a sender removed from the group, for example, is a permanent failure. Because the claim is transactional, a worker crash just rolls back, so there is no stuck `processing` state. Edit and cancel use `UPDATE … WHERE status='pending'` and return 409 if 0 rows change. `scheduled_at` is `timestamptz` (UTC) plus the user's IANA `timezone` column. `recurrence_rule` is nullable and `next_run_at` is separate from the original time, so recurring messages can be added later.
6. **Auth:** Argon2id via `argon2-cffi`. Access JWT is HS256 with a 15 min lifetime and claims `sub`, `exp`, `iat`, `jti`, `type`. The refresh token is an opaque random 256-bit value stored as SHA-256 in `refresh_tokens`, with rotation and `family_id` reuse detection: reuse revokes the whole family. The cookie is `Path=/api/v1/auth`, `SameSite=Strict`, with an extra `X-Requested-With` header check for CSRF. **WS auth:** the browser can't set headers, so the client gets a single-use ticket from `POST /api/v1/auth/ws-ticket` (lifetime about 30 s, stored hashed) and connects with `/ws?ticket=…`. This keeps the JWT out of URLs and logs. The server closes the socket with code 4001 when the ticket or access token becomes invalid, and 4003 on forbidden.
7. **Connection manager** (in the API process): `user_id → set[Connection]`, which handles multiple tabs. Fan-out is decided per user from the conversation membership cache, which is invalidated by `conversation.member_*` events, rather than by client-chosen subscriptions. The "active conversation" a client reports is used only to suppress notifications. Heartbeat is an app-level ping every 25 s with a 60 s idle timeout. Each connection gets a bounded send queue so one slow client cannot block fan-out (`asyncio.Queue` per connection, dropped if full → close 4008 → client resyncs).
8. **Presence:** the online state comes from the in-memory connection count and is broadcast on the 0↔1 transitions. `user_presence.last_seen_at` is persisted on the last disconnect. This works because there is a single API instance; the doc will describe how to move to multi-instance later.
9. **Search:** message full-text search uses a `tsvector` generated column with a GIN index. User search uses a `pg_trgm` GIN index on username and display_name. Results are always filtered to the caller's memberships.
10. **Notifications table** is used only for mentions, group add/remove, and scheduled-send failure. It is not written for every message, because unread counts already cover that.
11. **Soft delete:** messages get `deleted_at` and their content is nulled (a tombstone). Users get `deleted_at`. Conversations are never hard-deleted in v1. `audit_logs` is append-only and holds auth and security events plus admin-style mutations.
12. **Rate limiting and brute force:** an in-process token bucket, which the doc will note is good for a single instance only. Login lockout uses `failed_login_attempts` and `locked_until` on users, plus per-IP buckets.
13. **IDs:** UUID (`gen_random_uuid()`) primary keys. Events use a `bigserial` id. Messages order by `seq`. Pagination is keyset: `before_seq`/`after_seq` for messages and opaque cursors elsewhere.
14. **Python learning mapped to real needs:** context managers for the UoW/transaction and the worker lifespan; `async` generators for WS event streams and the sync replay; decorators for audit and log-context helpers; dataclasses for domain objects and events; a custom `AppError` hierarchy mapped to HTTP and WS errors; `contextvars` for the request and correlation id; `Protocol` for ports such as `EventPublisher`, `Clock` and `PasswordHasher`.

## Milestone outline (detailed in `docs/milestones/`)
M00 repo/tooling/compose/config/logging skeleton → M01 DB base + Alembic + users → M02 auth (register/login/refresh/logout/rotation) → M03 profiles + user search → M04 conversations (DM uniqueness, groups, members) → M05 messages REST (send/edit/delete/reply/history, idempotency, seq) → M06 outbox + LISTEN/NOTIFY + WS gateway (ticket auth, connection manager, fan-out) → M07 read cursors + unread + receipts → M08 presence + typing → M09 reconnect sync → M10 mentions + notifications → M11 scheduled messages + worker → M12 message search + audit + hardening. The frontend is built alongside, starting at M02, with M13 for frontend polish.

## Execution steps (after approval)
1. Write `docs/README.md` with the ADR list above.
2. Write `docs/design/01…15` in order. Each file follows the section numbering the spec requires. Use Mermaid for the ER and sequence diagrams, and tables for the endpoint and event contracts.
3. Write each `docs/milestones/Mxx-*.md` with all eight required fields.
4. Cross-check against the requirements: every numbered feature, every user scenario, every security, testing and logging bullet in the spec must be traced to a section. Put the traceability table in `docs/README.md`.

## Verification
- A traceability table in `docs/README.md` maps each requirement to a doc section, with no gaps.
- A grep for any Python code blocks confirms nothing beyond schemas, pseudo-SQL and JSON examples.
- Every milestone file contains all eight required headings, checked with a grep.
- The Mermaid diagrams render in the VS Code markdown preview.
