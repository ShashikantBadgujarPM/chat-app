# M12 — Message Search, Audit Coverage, Security Hardening

## Goal
Add full-text message search, restricted to the caller's memberships, with highlighted snippets. Complete the audit logging for security-relevant actions. Close the remaining security and observability items: the redaction test across every auth flow, security headers, OpenAPI contract snapshot, dependency audit, a rate-limit review, and production config checks. This is the "production readiness" pass before frontend polish.

## Dependencies
M05, M11.

## Files/modules expected
- `backend/app/modules/messaging/application/search_service.py`: `search_messages(user_id, q, conversation_id?, cursor, limit)` using `websearch_to_tsquery`, `ts_rank` ordering, `ts_headline` snippets (HTML-escaped by the server except for `<mark>`), and results limited to active memberships
- `backend/app/modules/messaging/api/search_router.py`: `GET /api/v1/search/messages`
- Audit: make sure `AuditLogger.record` is called for every action listed in [10 §20](../design/10-errors-logging-security.md) (login success and failure, lockout, reuse detection, session revocation, password change, account deletion, member add and remove, group rename, scheduled failure)
- `backend/tests/api/test_no_secrets_in_logs.py`: the cross-flow redaction test
- `backend/tests/api/test_openapi_snapshot.py` + `openapi.snapshot.json`
- `backend/tests/integration/test_constraint_registry.py`: every constraint name the code references exists in the migrated schema
- `frontend/nginx.conf` (production): CSP, HSTS, `nosniff`, `Referrer-Policy`, `frame-ancestors 'none'`, and the `/api` and `/ws` proxying
- `frontend/src/features/search/`: a search box (all conversations, or the current one), a results list with sanitized snippets (DOMPurify allowing only `<mark>`), and click-through to `messages/around/{seq}` with the message highlighted
- CI: `pip-audit`, `npm audit --omit=dev`

## Database changes
- `messages.search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(body, ''))) STORED` + `GIN(search_vector)`. On a large existing table this migration rewrites the table, which is noted in the migration docstring, but it's fine at dev scale.
- Hardening: `REVOKE UPDATE, DELETE ON audit_logs FROM chat_app`.

## API changes
- `GET /api/v1/search/messages?q=&conversation_id=&cursor=&limit=` → `{items: [{message, conversation_id, snippet_html, rank}], next_cursor}`.
- OpenAPI docs (`/docs`, `/openapi.json`) are disabled when `ENV=production`.

## WebSocket changes
None functionally. Review item: confirm the per-connection rate limits and frame size limits are applied, and add the `ws.stats` periodic log line (connections, users online, queue high-water mark).

## Tests
- API: search finds stemmed words ("deploying" matches "deployment"); a quoted phrase and `-exclusion` work; hostile input (`'`, `"`, `:*`, `&|!()`, 10 KB) → 422 or empty results, never 500; a message from a conversation the user left isn't returned; a deleted message isn't returned; a `conversation_id` filter for a conversation the user can't see → 404.
- API: `snippet_html` contains no tags other than `<mark>`, even when the message body contains `<script>`.
- Redaction: register, login, refresh, ws-ticket, password change → none of the raw password, access token, refresh token or ticket appears in any captured log record.
- Audit: each listed action writes exactly one audit row with the expected `action`, with no secrets in `metadata`; `chat_app` can't UPDATE or DELETE `audit_logs`.
- Config: `ENV=production` with an insecure setting fails at startup (this extends the M00 tests).
- OpenAPI snapshot matches (an intentional change requires updating the snapshot in the same PR).

## Acceptance criteria
- Searching "deployment" finds Rahul's message across conversations; clicking a result jumps to it in context with it highlighted.
- A security checklist review of [10 §20](../design/10-errors-logging-security.md) is written up in the PR, with a status for each row (done / accepted limitation).
- `pip-audit` and `npm audit` have no high or critical findings, or the exceptions are documented.
- A production-mode compose run (`ENV=production`, the nginx frontend) works end to end, and the security headers are present.
