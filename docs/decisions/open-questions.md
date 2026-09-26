# Open Questions and Resolved Decisions

The implementation agent records spec ambiguities here ([15 §26.3](../design/15-sonnet-strategy.md)). Each entry gives the question, the resolution, and where the docs were updated. Items marked **OPEN** block the milestone that depends on them.

## Resolved during the M00 review (2026-09-26)

### Q-001 Health endpoints and the `/api` prefix
- **Question:** 07, 12, M01 and M06 put health at `/health/*`, but M00's acceptance criterion checked `/api/health/live` through the Vite proxy. The M00 code added a path rewrite to satisfy it.
- **Resolution:** health stays at `/health/*`, outside `/api/v1`. Vite proxies `/health` with the path unchanged, and there is no rewrite. Orchestrators call the API directly, so production nginx needs no health rule.
- **Updated:** M00 acceptance criterion and file list, [12 §22.3](../design/12-local-dev-docker.md), `frontend/vite.config.ts`, `frontend/src/App.tsx`.

### Q-002 Error envelope for 500 responses
- **Question:** 10 §18 showed a flat `{code, message, request_id}` 500 body, while 07 §13.1 wraps every non-2xx response in `{"error": {...}}`.
- **Resolution:** 07 is canonical. Every non-2xx response, including 500 and the readiness 503, uses `{"error": {"code", "message", "details", "request_id"}}`.
- **Updated:** [10 §18](../design/10-errors-logging-security.md). No code change was needed.

### Q-003 When `COOKIE_SECURE=false` is allowed
- **Question:** 12 allowed it only when `ENV=development`; 10 §20 and M00 only said production rejects it.
- **Resolution:** development only, as in 12. Tests therefore keep Secure cookies and use an `https://test` base URL for httpx, which is noted in M02.
- **Updated:** [10 §20](../design/10-errors-logging-security.md), M00 tests line, M02 tests.

## Resolved during the M01 review (2026-09-26)

### Q-004 Where the `chat_app` grants live
- **Question:** 12 §22.5 put roles and grants in a Postgres init script, which runs once for one database. Test databases created per xdist worker would have no grants.
- **Resolution:** the init script (`db/init/01-roles.sh`) creates only the cluster-level `chat_app` login role. Migration 0001 applies the grants, the default privileges and the role's `idle_in_transaction_session_timeout`, so every migrated database gets them.
- **Updated:** [12 §22.5](../design/12-local-dev-docker.md), M01.

### Q-005 Test database source and role
- **Question:** 11 said testcontainers only, but 12 also runs `pytest` inside the api container, where testcontainers can't start Docker. The connecting role wasn't specified.
- **Resolution:** use `TEST_DATABASE_ADMIN_URL` if it is set, otherwise start a testcontainer. Application sessions connect as `chat_app`; only setup, migrations and truncation use the owner.
- **Updated:** [11 §21.3](../design/11-testing.md), M01.

### Q-006 Trigram index on a `citext` column
- **Question:** `pg_trgm` has no operator class for `citext`, so 05's `GIN (username gin_trgm_ops)` fails.
- **Resolution:** use the expression index `GIN ((username::text) gin_trgm_ops)`, and make search query exactly `username::text`.
- **Updated:** [05 §users](../design/05-database.md), M03.

## Found while implementing M01 (2026-09-26)

### Q-007 Reading constraint names from the driver
- **Question:** 10 §18 said to match on `e.orig.diag.constraint_name`, which is psycopg's API. The app uses asyncpg, whose exception carries `constraint_name` directly.
- **Resolution:** a single helper, `app.platform.db.constraint_name(exc)`, handles both drivers. All code that matches expected violations uses it.
- **Updated:** [10 §18](../design/10-errors-logging-security.md).

### Q-008 Alembic's async psycopg driver on Windows
- **Question:** async psycopg can't run on Windows' default `ProactorEventLoop`, so `alembic upgrade` failed when run on a Windows host.
- **Resolution:** `alembic/env.py` passes `loop_factory=asyncio.SelectorEventLoop` to `asyncio.run` on Windows only. The API (asyncpg) and the Linux containers are unaffected. This is an implementation detail with no design change.

### Q-009 Test server sharing under xdist
- **Question:** 11 §21.3 says "start one Postgres container" with a database per xdist worker. Each xdist worker is a separate process, so sharing one container would need cross-process coordination.
- **Resolution (implementation):** without `TEST_DATABASE_ADMIN_URL`, each xdist worker starts its own testcontainer. With it, all workers share that server. Migrations are serialized with an advisory lock taken in the `postgres` database, because concurrent `ALTER ROLE` statements from several workers raised "tuple concurrently updated". Migration 0001 also skips `ALTER ROLE` when the setting already exists. Revisit if CI container start-up time becomes a problem.

## Found while implementing M02 (2026-09-26)

### Q-010 Lockout timestamps come from the Clock port
- **Question:** R-18 writes the lockout with DB `now()`, but M02 and 11 §21.4 require testing "unlock after the window" with a fake clock, which can't move DB `now()`.
- **Resolution:** the atomic `UPDATE … RETURNING` from R-18 is kept, so increments still can't be lost, but it binds `:now` from the `Clock` port. The lock check and the refresh grace window compare against the same clock. With a single API instance there is no clock-skew concern between writers. JWT time checks also use the injected clock, not PyJWT's wall clock.
- **Updated:** none (implementation detail; R-18's guarantee is unchanged).

### Q-011 Failures that must persist are committed before the error is raised
- **Question:** the failed-login counter, reuse-detection family revocation and their audit rows must survive a failing request, but raising inside `async with uow:` rolls them back.
- **Resolution:** those use cases return the error from inside the transaction and raise it after the commit (`Login`, `RefreshSession` in `identity/application/services.py`). A unit test with a staging fake UoW pins this.
- **Updated:** none (pattern documented in the module docstring).
