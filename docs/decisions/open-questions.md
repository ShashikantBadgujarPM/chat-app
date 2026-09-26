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
