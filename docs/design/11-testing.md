# 21. Testing Architecture

## 21.1 Principles

- **Real PostgreSQL in integration, API and WS tests.** No SQLite. Most of the hard guarantees in this system live in Postgres: `SKIP LOCKED`, partial and GIN indexes, `citext`, `tsvector`, NOTIFY-on-commit and constraint-name matching. A fake database would give false confidence exactly where the risk is.
- **Test pyramid by cost:** many fast unit tests (domain and application with fakes), a solid layer of integration tests (repositories, UoW, SQL), API tests for each endpoint contract, and a focused set of WS and concurrency tests.
- **Deterministic time.** Every "now" in application code comes from the `Clock` port or from DB `now()`. Tests inject a `FrozenClock`. For DB-time-dependent cases (scheduler), tests insert rows relative to DB `now()` (for example `scheduled_at_utc = now() - interval '1 second'`), so they never sleep.
- **Every test is independent** and order-agnostic, and runs in parallel under `pytest-xdist` (a database per worker; see §21.3).

## 21.2 Tooling

`pytest`, `pytest-asyncio` (asyncio mode `auto`), `httpx.AsyncClient` with `ASGITransport` for API tests, `testcontainers[postgres]` (or the compose `db` service in CI), `pytest-xdist`, `pytest-cov` (target ≥ 85% on `application` and `domain`), `freezegun`-style clock fakes through our own `Clock` port rather than monkeypatching `datetime`, and Starlette's `TestClient` WebSocket support for protocol tests. A real uvicorn server on an ephemeral port plus the `websockets` client library is used for the few tests that need true concurrency across connections.

## 21.3 Database fixtures

- **Session scope:** pick the Postgres server. If `TEST_DATABASE_ADMIN_URL` is set (a superuser URL, as in CI or inside the api container against the compose `db`), use that server; otherwise start one testcontainer. Ensure the `chat_app` role exists (create it with a random password if missing), then for each xdist worker create database `test_<worker>` and run `alembic upgrade head` once as the owner, so the **migrations themselves are tested**, including the grants, not `metadata.create_all()`.
- **Roles in tests:** application sessions connect as **`chat_app`**, the same role production uses, so a missing grant or an accidental DDL fails a test. Only migrations, database setup/teardown and the `real_commits` truncate use the owner connection.
- **Function scope, default:** wrap each test in a connection-level transaction with the session bound to a SAVEPOINT, and roll back at the end (the standard SQLAlchemy "join an external transaction" recipe). This is fast, and isolation is automatic.
- **Function scope, `@pytest.mark.real_commits`:** for tests that need *committed* data visible across connections: concurrency tests, the NOTIFY listener, and the scheduler with multiple workers. These use real commits followed by `TRUNCATE … RESTART IDENTITY CASCADE` of all tables in teardown. They are slower, so they are used only where required.
- **Factories:** small async factory functions in `tests/factories.py` (`make_user`, `make_conversation(members=…)`, `make_message`, `make_scheduled(due_in=…)`), plain functions with keyword defaults. No factory library is needed, which is a readable Python idiom for a Node developer.
- **Auth helper:** `auth_headers(user)` mints an access token directly through `TokenIssuer`, so tests don't have to log in over HTTP.

## 21.4 What each layer covers

### Unit (`tests/unit/`, no I/O, < 1 ms each)
- Domain: `ScheduledMessage.can_edit/can_cancel` for every status; the conversation invariants (DM pair uniqueness key computation, last-owner removal); mention parsing (edge cases: `email@x.com` is not a mention, `@@user`, punctuation, Unicode, duplicates, at most 50 mentions); body trimming and limits.
- Application services with in-memory fakes of the repository and publisher ports: correct events are emitted with the correct recipients; authorization errors are raised for non-members and non-senders; the idempotent-replay path returns the existing entity.
- Retry backoff function: bounds, jitter range, cap.
- Error mapping: every `AppError` subclass maps to the documented HTTP status and code (a parametrized table test mirrors the table in [10](10-errors-logging-security.md)).
- Token issuer: expiry, `aud`/`iss`/`type` checks, and rejection of the `alg=none` and HS256-with-wrong-key cases.
- Redaction filter.

### Integration (`tests/integration/`, real DB)
- Every repository method, including keyset pagination boundaries (empty; exactly `limit`; `limit+1`; `before_seq=1`).
- `seq` assignment: 50 concurrent sends to one conversation give seqs 1..50 exactly, with no gaps and no duplicates (`real_commits`).
- DM creation race: 20 concurrent `create_direct(A,B)` calls produce 1 conversation, and all callers get the same id.
- The outbox trigger fires NOTIFY only on commit: a rolled-back transaction produces no notification.
- Full-text search: ranking, `websearch_to_tsquery` with hostile input (`'`, `"`, `:*`, `&|!`) never raises, and results are membership-scoped.
- Constraint names match what the error-translation code expects. A test lists every constraint the code references by name and asserts that it exists in the migrated schema, so renaming a constraint without updating the code breaks the build.
- Alembic: `upgrade head` → `downgrade base` → `upgrade head` round trip on an empty DB.

### API (`tests/api/`, httpx against the app)
- Per endpoint: the happy path (status, response schema), each documented error `code`, the auth-required check, and the authorization check (another user's resource returns 404/403 as documented).
- Contract snapshot: the generated OpenAPI schema is compared with a committed snapshot, so accidental contract changes show up in review.

### Authentication (`tests/api/test_auth_*.py`)
- Register (duplicates are case-insensitive; weak password), login success and failure, uniform failure responses for unknown user versus bad password.
- Lockout after 5 failures, `423` with `Retry-After`, unlock after the window (fake clock).
- Refresh rotation: the old cookie is revoked and the new cookie works.
- **Reuse detection:** use an old token after the 10 s grace period → 401 and the whole family is revoked, so the new token fails too.
- **Benign race:** two concurrent refreshes with the same cookie → one 200 and one 409 `refresh_superseded`, and the family is *not* revoked.
- Logout revokes, logout-all revokes everything, and a password change revokes the other sessions.
- Access token: expired, bad signature, wrong `type` (a refresh token used as an access token), missing header, disabled user → all 401.
- Cookie attributes: HttpOnly, Secure (prod settings), SameSite=Strict, Path. Refresh without `X-Requested-With` → 403.

### Authorization (`tests/api/test_authz_matrix.py`)
A **parametrized matrix test** over (endpoint × actor role). The roles are non-member, former member, member, sender, non-sender member, owner and non-owner. Each combination is asserted against the policy table in [06](06-auth-and-authorization.md) §12, so the table *is* the test spec.

### WebSocket (`tests/ws/`)
- Handshake: a valid ticket connects and gets `hello`. Invalid, expired or already-consumed tickets → close 4001. A bad Origin is rejected.
- A ticket can be used only once, even with 2 concurrent connects racing on it.
- `message.created` reaches all members' connections, including the sender's other connection, and never reaches a non-member.
- Multi-tab: the same user with 3 connections gets the event on all 3. Presence stays online until the last one closes, then goes offline after the grace period (fake clock or a short configured grace).
- Typing: delivered to others and not echoed to the sender; a non-member's typing frame gets an `error`; the throttle is applied; nothing is written to the DB (assert the row counts in `event_outbox` are unchanged).
- Sync: disconnect, generate N events over REST, reconnect with `after_event_id` → exactly those events in order, then `sync.complete`, then live events. Events generated *during* the sync replay are delivered exactly once, through the buffering. A pruned cursor → `sync.reset_required`.
- **Out-of-order commit:** open T1 and insert outbox event id=100 without committing; T2 inserts and commits id=101; the client receives 101, disconnects, T1 commits, and the client syncs from 101 → it receives 100 (the overlap window) and does not re-apply 101 (the client dedup contract is verified through the test client's own dedup helper).
- Slow consumer: a client that never reads → the server closes it with 4008 once the queue fills, and other clients keep receiving (`real server` test).
- `session.revoked`: logout from session S closes S's sockets with 4001, and sockets from session S2 stay open.
- The listener reconnect path: kill the listener connection (`pg_terminate_backend`) → the connected clients get `sync.required`.
- Malformed frames: non-JSON, an unknown type, or an oversized frame → `error`. A flood of invalid frames → close 4000.

### Scheduler (`tests/scheduler/`)
- Due rows are delivered; not-yet-due rows aren't; cancelled rows are never delivered.
- Delivery creates exactly one message with the right `seq`, the mention notifications, the `message.created` and `scheduled_message.sent` outbox events, and an audit row, **all in one transaction**. The test injects a failure after the message insert and asserts nothing was persisted.
- Permanent failures: sender removed from the group → `failed` with `sender_not_member`, a notification to the sender, and no message. Overdue beyond max lateness → `failed` with `expired`.
- Transient failures: the injected `OperationalError` → `attempts=1`, `next_attempt_at` pushed back by the backoff, still `pending`. Max attempts → `failed`.
- A savepoint per item: in a batch of 3 where item 2 fails, items 1 and 3 are sent and item 2 is set to retry.
- Restart: insert due rows, start a fresh worker instance → they are delivered (there is no in-memory state to lose).
- Retry endpoint: a `failed` message goes back to `pending` and is delivered.

### Concurrency and race conditions (`tests/concurrency/`, `real_commits`)
Each test uses `asyncio.gather` across **separate DB connections or engines**, so the database sees real concurrency rather than interleaving on one connection.

| Test | Asserts |
|---|---|
| N workers × M due rows (e.g. 4 × 200) | Exactly M messages; `COUNT(*) GROUP BY scheduled_message_id` is ≤ 1 everywhere; all rows are `sent` |
| Cancel vs worker | Worker locks the row (a test hook pauses after the claim), cancel is issued → it blocks, the worker commits → cancel gets 409. The reverse order: cancel first → the worker never delivers |
| Edit vs worker | The same two orderings, with edit |
| Concurrent sends to one conversation | seq is contiguous and unique |
| Concurrent DM creation | One conversation |
| Concurrent add-member of the same user | One membership row, and both calls succeed (idempotent) |
| Concurrent read-cursor PUTs out of order | The final cursor = the max (it never goes backward) |
| Concurrent refresh with the same cookie | See the auth tests |
| Last-owner race: two owners remove each other at the same time | At least one owner remains (the invariant is enforced under `SELECT … FOR UPDATE` on the owner rows) |
| Ticket double-consume | One connection succeeds |

**Deterministic interleaving:** tests that need "worker is holding the lock *right now*" use an `asyncio.Event`-based hook injected into the worker (`on_claimed` callback, a no-op in production) instead of `sleep`. The test waits for the event, performs the racing action, then releases the worker. That makes race tests reproducible instead of flaky, which is a key lesson in its own right.

### Failure and retry
Covered above for the scheduler. In addition: a DB outage mid-request → 500 with a request id and nothing half-committed; the outbox listener survives a DB restart (container restart in a nightly or manual test, documented as a manual chaos check in v1).

## 21.5 Frontend tests (lighter weight)

Vitest + React Testing Library for components and hooks. The **WS client state machine** (connect → hello → sync → live → reconnect with backoff, dedup, seq-gap detection, close-code handling) is the important unit to test, against a mock socket. One Playwright smoke test runs against the compose stack: two browser contexts exchange a message, and it appears in real time in the other.

## 21.6 CI pipeline

1. `ruff check` + `ruff format --check`, `mypy --strict` on `app/`.
2. Unit tests (no DB).
3. Start Postgres → integration, API, WS, scheduler and concurrency tests with coverage.
4. `pip-audit`, `npm audit --omit=dev`, frontend `tsc --noEmit` + Vitest.
5. Build the Docker images.
