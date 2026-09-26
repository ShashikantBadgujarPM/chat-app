# 18–20. Error Handling, Logging/Observability, Security Architecture

## 18. Error handling

### 18.1 One hierarchy, two translators

The exception tree is defined in [04-domain-model.md](04-domain-model.md) §8.3. Domain and application code **raises**. Only two places **translate**:

1. **REST:** FastAPI exception handlers registered in `platform/errors.py`.
2. **WS:** the gateway's frame-dispatch loop. For errors caused by a client frame it sends an `error` frame; for connection-level failures it closes with the matching close code.

Route handlers and services contain no `try/except` just to reformat errors. That rule keeps error shapes consistent and is easy to check in code review.

| Exception | HTTP | `code` examples | WS behavior |
|---|---|---|---|
| `ValidationError` (ours) / Pydantic `RequestValidationError` | 422 | `body_too_long`, `naive_datetime`, `invalid_timezone` | `error` frame |
| `ConflictError` | 409 | `not_pending`, `message_deleted`, `refresh_superseded` | `error` frame |
| `InvariantViolation` | 409 | `cannot_remove_last_owner` | `error` frame |
| `NotFoundError` | 404 | `conversation_not_found`, `message_not_found` | `error` frame |
| `AuthenticationError` | 401 (with `WWW-Authenticate: Bearer`) | `invalid_token`, `token_expired`, `invalid_credentials` | close 4001 |
| `AccountLockedError` (subclass of AuthenticationError) | 423 + `Retry-After` | `account_locked` | n/a |
| `AuthorizationError` | 403 | `not_owner`, `not_sender` | `error` frame; close 4003 if repeated |
| `RateLimitedError` | 429 + `Retry-After` | `rate_limited` | frames dropped; close 4029 on sustained abuse |
| Anything else | 500 | `internal_error` | close 1011 |

**500 handling:** the handler logs the exception with its stack trace at ERROR, including `request_id`, method, route template and user id. It returns only the standard envelope from [07 §13.1](07-rest-api.md): `{"error":{"code":"internal_error","message":"An unexpected error occurred.","details":null,"request_id":…}}`. It never returns stack traces, SQL or exception text, whatever the environment; in dev the stack trace goes to the console log, not the response.

**Database errors:**
- **Expected constraint violations** are caught *in the repository or service that expects them* and turned into domain errors. Examples: a unique violation on `direct_key` means the DM already exists, so the existing one is returned; a unique violation on `client_message_id` means an idempotent replay. They are matched by **constraint name** (`e.orig.diag.constraint_name`), never by parsing the message text. Constraint names are therefore set explicitly in the SQLAlchemy models (a naming convention in `MetaData`), which is a good habit to learn anyway.
- **Unexpected** DB errors propagate to the 500 handler.

**Transactions and errors:** the `UnitOfWork` context manager commits on normal exit and rolls back on any exception, then re-raises. Services never call `commit()` directly. One request is one UoW, except for the few use cases that are explicitly multi-transaction, such as the worker's batch loop. Because the transaction had already committed before the handler ran, a response serialization failure after commit still results in a correct DB state; the client retries with the same `client_message_id` and gets the original result back.

## 19. Logging and observability

### 19.1 Setup

Standard library `logging`, configured once in `platform/logging.py` through `logging.config.dictConfig`:
- **Production (`LOG_FORMAT=json`):** one JSON object per line on stdout, using a small custom `JsonFormatter`. It could also be `python-json-logger`; the choice is left to the implementer, as long as the output field names match the table below. Docker or the host log shipper collects stdout.
- **Development (`LOG_FORMAT=console`):** human-readable key=value output.
- The level comes from `LOG_LEVEL`, with per-logger overrides: `sqlalchemy.engine` at WARNING (INFO echoes SQL with parameters, which can contain PII), and `uvicorn.access` **disabled**, because our own access-log middleware replaces it.

### 19.2 Context propagation

A `contextvars.ContextVar` holds a `LogContext` (`request_id`, `user_id`, `connection_id`, `batch_id`). A `logging.Filter` copies these onto every `LogRecord`. Since contextvars follow async tasks, every log line inside a request, including those from deep in a repository, carries the request id without passing it as a parameter. This is a good example of why `contextvars` exists.

- **REST:** a pure ASGI middleware reads `X-Request-ID`, accepting it only if it matches `^[A-Za-z0-9._-]{8,64}$` (otherwise a new one is generated, so log injection isn't possible). It sets the context and echoes the header. After auth resolves, a dependency adds `user_id`.
- **WS:** the context is set per connection (`connection_id`, `user_id`). Each inbound frame gets a derived `request_id` (`<connection_id>:<n>`).
- **Worker:** the context is set per batch (`batch_id`).
- **Outbox events:** these store `correlation_id` from the context at write time. The listener sets it when delivering, so fan-out logs connect back to the originating request, even when that request ran in the worker process.

### 19.3 Standard fields

`ts` (UTC ISO8601), `level`, `logger`, `event` (a stable snake/dot-case event name such as `auth.login_failed`), `msg`, `request_id`, `user_id`, `connection_id`, `batch_id`, `service` (`api`|`worker`), `env`, `version` (git sha), and `exc_info` (formatted stack trace, present on exceptions). Event-specific fields go in as structured `extra`, never interpolated into `msg`.

### 19.4 What gets logged

| Area | Events (level) |
|---|---|
| HTTP | `http.request` (INFO): method, **route template** (`/conversations/{id}`, not the raw path), status, duration_ms, user_id. 5xx → ERROR |
| Auth/security | `auth.registered`, `auth.login_succeeded` (INFO); `auth.login_failed` (WARNING: reason `bad_password`/`unknown_user`/`locked`, username **hash**, ip); `auth.account_locked`, `auth.refresh_reuse_detected` (WARNING, and also written to `audit_logs`); `auth.logout`, `auth.token_invalid` (INFO/DEBUG) |
| WS lifecycle | `ws.connected` (INFO: connection_id, user_id, user_agent), `ws.disconnected` (INFO: close_code, duration_s, frames_in/out), `ws.auth_failed` (WARNING), `ws.slow_consumer_closed` (WARNING), `ws.sync_completed` (INFO: replayed, duration_ms, reset), `ws.listener_reconnected` (WARNING) |
| Messaging | `message.created` / `updated` / `deleted` (INFO: message_id, conversation_id, seq, body_length). **Never the body** |
| Scheduler | See [09](09-scheduled-messages.md) §17.5 |
| Lifecycle | `app.startup` / `app.shutdown` (INFO: version, config summary without secrets) |

### 19.5 Redaction (defense in depth)

1. **By construction:** passwords, tokens, tickets, cookies and `Authorization` headers are never passed to loggers. Request and response bodies are never logged.
2. **Filter:** a `RedactingFilter` masks values of keys matching `password|token|secret|authorization|cookie|ticket|refresh` in `extra` dicts, and masks anything that looks like a JWT (`eyJ[\w-]+\.[\w-]+\.[\w-]+`) in `msg`.
3. **Test:** a pytest test drives login, refresh and ws-ticket through the API with a capturing log handler, and asserts that the raw password, access token, refresh token and ticket never appear in any captured record.

Usernames on failed logins are logged as a truncated SHA-256 hash. That still lets you correlate brute-force attempts against one account without writing possibly-mistyped passwords (people sometimes type their password into the username field) to the logs.

### 19.6 Metrics (lightweight, v1)

No Prometheus in v1, but the design leaves room for it. The periodic `scheduler.stats` and `ws.stats` log lines (connections, users online, send-queue high-water mark, events delivered per minute) give the key signals. `/health/ready` covers liveness of the dependencies. Adding `prometheus-client` later means wrapping these same counters.

## 20. Security architecture

| Concern | Control |
|---|---|
| **Password hashing** | Argon2id (argon2-cffi), rehash-on-login when parameters change, run off the event loop with `asyncio.to_thread`, 10–128 char policy with a common-password check. See [06](06-auth-and-authorization.md) §11.4 |
| **JWT security** | HS256 with a secret of at least 32 random bytes from the environment; `algorithms=["HS256"]` pinned; `exp`/`iss`/`aud`/`type` validated; 15 min lifetime; no sensitive claims. The `sid` claim binds the token to a session. Secret rotation: support `JWT_SECRET_PREVIOUS` for verification during a rotation window |
| **Refresh-token security** | Opaque 256-bit random value, stored as SHA-256 (a fast hash is fine for high-entropy tokens; Argon2 is only needed for low-entropy passwords); rotation on every use; family reuse detection; httpOnly+Secure+SameSite=Strict cookie scoped to `/api/v1/auth` |
| **Token revocation** | Refresh tokens: `revoked_at` per token, per family (logout), or all (logout-all, password change, account deletion). Access tokens expire within 15 min; WS sessions close immediately through `session.revoked` |
| **Brute force** | Per-account lockout (5 failures → 15 min), per-IP token bucket, uniform 401 responses with dummy-hash timing equalization. See [06](06-auth-and-authorization.md) §11.5 |
| **Authorization** | Relationship-based membership checks at both the route dependency and the service layer; membership constraints expressed inside queries; 404 for resources the caller can't see. See [06](06-auth-and-authorization.md) §12 |
| **IDOR** | Every item route resolves the resource *through* the caller's memberships (`JOIN conversation_members … WHERE user_id = :me`). UUIDv4 ids are unguessable, but that is **not** relied upon |
| **WebSocket authentication** | Single-use 30 s ticket; atomic consume; Origin allow-list; session-bound, with revocation closing sockets. See [08](08-websocket.md) §14.2 |
| **Message authorization** | Send, edit, delete and reply are checked in the service. Recipients are computed by the server from membership and frozen into the outbox row. Typing frames are checked against membership |
| **Input validation** | Pydantic v2 models with `extra="forbid"`, `constr` limits (body ≤ 4000, title ≤ 100, username `^[A-Za-z0-9_]{3,32}$`), timezone-aware datetimes only, UUID-typed path params, `limit` caps. Body text is stored as-is (no HTML sanitizing on input), and **output encoding is the frontend's job**: React escapes text by default, and `dangerouslySetInnerHTML` is used only for search snippets, through DOMPurify with only `<mark>` allowed |
| **SQL injection** | SQLAlchemy Core/ORM with bound parameters everywhere. Raw SQL (claim query, search) uses `text()` with bind params only. Search input goes through `websearch_to_tsquery`, which never interprets it as SQL. Sort and order fields come from allow-lists, never interpolated. A lint rule or review check rejects f-strings passed to `text()` |
| **Sensitive logging** | See §19.5 |
| **Rate limiting** | In-process token buckets keyed by IP or user, as a FastAPI dependency plus a WS per-connection bucket. Limits are in [07](07-rest-api.md) §13.4. **Limitation:** they are per process, which is correct only while there is one API instance; moving to Redis is the documented upgrade. `X-Forwarded-For` is trusted only from configured proxy IPs (`TRUSTED_PROXIES`), otherwise IP-based limits can be spoofed |
| **CORS** | `allow_origins` = an explicit list from config (never `*` with credentials), `allow_credentials=True` (needed for the refresh cookie), methods and headers restricted to what's used. In dev, the Vite proxy makes the app same-origin, so CORS is mostly a production concern |
| **CSRF** | Only `/auth/refresh` and `/auth/logout` accept cookie auth. SameSite=Strict, path scoping and the required `X-Requested-With` header protect them. Everything else is bearer-only and therefore not CSRF-able |
| **Security headers** | Set by the frontend's nginx in production: `Content-Security-Policy` (`default-src 'self'; connect-src 'self' wss://<host>`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Strict-Transport-Security`, `frame-ancestors 'none'` |
| **Secure configuration** | `pydantic-settings` `Settings` class; it **fails fast at startup** if `JWT_SECRET` is missing or shorter than 32 bytes, if `COOKIE_SECURE=false` outside `ENV=development` (see [12 §22.3](12-local-dev-docker.md)), or if `ENV=production` and `DEBUG=true` or `CORS` contains `*`. OpenAPI docs (`/docs`) are disabled in production |
| **Secret management** | Secrets only come from environment variables (or `*_FILE` variants for Docker secrets). `.env` is git-ignored; `.env.example` is committed with placeholder values. No secrets in images or compose files beyond dev defaults clearly marked as such. The DB app role has no superuser rights; migrations run under a separate owner role (see [12](12-local-dev-docker.md)) |
| **Audit** | `audit_logs` is append-only (login failures, lockouts, reuse detection, session revocations, membership changes, scheduled failures). Hardening: `REVOKE UPDATE, DELETE ON audit_logs FROM app_role` |
| **DoS basics** | Max request body 64 KB (middleware), max WS frame 16 KB, bounded WS send queues, statement timeouts, a cap on pending scheduled messages per user, group size cap of 100, pagination limits |
| **Dependencies** | Pinned lockfile (`uv.lock` or `requirements.txt` with hashes); `pip-audit` and `npm audit` in CI |
