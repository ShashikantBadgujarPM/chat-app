# 11–12. Authentication Architecture and Authorization Model

## 11. Authentication

### 11.1 Token model

| Token | Form | Lifetime | Where the browser keeps it | Where the server keeps it |
|---|---|---|---|---|
| Access token | JWT, HS256, signed with `JWT_SECRET` | 15 min | JS memory only (module variable / React context) — never `localStorage` | Not stored (stateless) |
| Refresh token | Opaque, 256-bit random (`secrets.token_urlsafe(32)`) | 14 days, sliding via rotation | `httpOnly; Secure; SameSite=Strict; Path=/api/v1/auth` cookie named `rt` | SHA-256 hash in `refresh_tokens` |
| WS ticket | Opaque, 256-bit random | 30 s, single use | Passed once as `?ticket=` on the WS URL | SHA-256 hash in `ws_tickets` |

**Access-token claims:** `sub` (user id), `sid` (the refresh-token `family_id` = the login session), `iat`, `exp`, `jti` (UUID, for log correlation — not a denylist key in v1), `type: "access"`, `iss: "chat-app"`, `aud: "chat-app-api"`. Verification pins `algorithms=["HS256"]` (never trusts the header's `alg`), and checks `exp`, `iss`, `aud`, and `type`.

**Why access tokens are not revocable in v1:** a 15-minute lifetime bounds exposure; logout revokes the refresh token so no new access token can be minted. Immediate revocation (e.g. on password change) is handled by storing a `users.token_version`-style check only if needed later — noted as a deliberate tradeoff, not an oversight. Disabled accounts (`users.status='disabled'`) are rejected on every request anyway, because `get_current_user` loads the user row (one indexed PK lookup), so disablement takes effect immediately regardless of token lifetime.

### 11.2 Why an httpOnly cookie for refresh, but a bearer header for access

- The refresh token is the long-lived secret — putting it in an `httpOnly` cookie means XSS cannot exfiltrate it.
- The access token in a header (not a cookie) means ordinary API calls are **not** CSRF-able: a cross-site form can't set `Authorization`.
- The only cookie-authenticated endpoints are `/auth/refresh` and `/auth/logout`. These are protected from CSRF by (a) `SameSite=Strict`, (b) `Path=/api/v1/auth` scoping, and (c) requiring a custom header `X-Requested-With: chat-app` which cross-site forms cannot send without a CORS preflight that our CORS policy rejects for foreign origins. Defense in depth — any one of these is usually sufficient.

### 11.3 Flows

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as /api/v1/auth
    participant DB as PostgreSQL

    Note over B,DB: Login
    B->>API: POST /login {username, password}
    API->>DB: SELECT user; check locked_until
    API->>API: argon2.verify (constant-time); on fail increment failed_login_attempts
    API->>DB: INSERT refresh_tokens (new family_id, hash)
    API-->>B: 200 {access_token, expires_in, user} + Set-Cookie rt=...

    Note over B,DB: Refresh (rotation)
    B->>API: POST /refresh (cookie rt, X-Requested-With)
    API->>DB: SELECT ... WHERE token_hash=sha256(rt) FOR UPDATE
    alt token valid & not revoked
        API->>DB: UPDATE old SET revoked_at=now(), replaced_by_id=new; INSERT new (same family)
        API-->>B: 200 {access_token} + Set-Cookie rt=new
    else token already revoked (REUSE DETECTED)
        API->>DB: UPDATE refresh_tokens SET revoked_at=now() WHERE family_id=... AND revoked_at IS NULL
        API->>DB: INSERT audit_logs 'auth.refresh_reuse_detected'
        API-->>B: 401 + clear cookie
    end

    Note over B,DB: Logout
    B->>API: POST /logout (cookie rt)
    API->>DB: UPDATE refresh_tokens SET revoked_at=now() WHERE token_hash=...
    API-->>B: 204 + clear cookie
```

**Refresh-token reuse detection** is the key security property: if an attacker steals a refresh token and uses it after the legitimate client already rotated it (or vice versa), the second use presents an already-revoked token → the entire `family_id` chain is revoked, logging out both the attacker and the victim, and forcing re-login. **Benign concurrent refresh (multi-tab) vs. real reuse:** `SELECT ... FOR UPDATE` on the token row serializes two simultaneous refreshes with the same cookie, but on its own the second one would still see a revoked token and trigger a false family revocation. Therefore: if the presented token was revoked **less than 10 s ago and has a `replaced_by_id`** (i.e. it was rotated, not logged out), the server returns `409 {"code":"refresh_superseded"}` *without* revoking the family. Because the cookie jar is shared across tabs, the client simply retries once and now sends the successor cookie. Outside that window, reuse is treated as theft. The frontend also avoids the race in the first place with a single-flight refresh lock (`navigator.locks.request('refresh', …)`) and broadcasts the new access token to other tabs over `BroadcastChannel`. See [13](13-failure-and-concurrency.md) §R-6.

**Logout-all-sessions:** `POST /auth/logout-all` revokes every non-revoked refresh token for the user. Exposed because the sessions list (`GET /auth/sessions`) makes it natural and it's the standard response to "I think my account was compromised."

### 11.4 Password hashing

Argon2id via `argon2-cffi`'s `PasswordHasher` with its current defaults (time_cost=3, memory_cost=64 MiB, parallelism=4 as of argon2-cffi 23.x — verify against the installed version at implementation time and pin parameters in config). On successful login, `check_needs_rehash()` is called and the hash is transparently upgraded if parameters changed — a real-world concern that's easy to forget. Password policy: minimum 10 characters, maximum 128 (bounds hashing cost / DoS), no composition rules (per NIST SP 800-63B), checked against a small local list of the most common passwords.

Argon2 verification is CPU/memory bound and would block the event loop; it runs via `await asyncio.to_thread(hasher.verify, ...)` — a genuine, non-contrived lesson about the difference between async I/O and CPU-bound work in Python.

### 11.5 Brute-force protection

- Per-account: `failed_login_attempts` incremented on each failure; after 5 consecutive failures, `locked_until = now() + 15 min` (exponential on repeated lockouts is a possible later enhancement). Reset to 0 on success.
- Per-IP: in-process token bucket (10 login attempts / minute / IP) via the `rate_limit` dependency.
- **Uniform failure response:** unknown username and wrong password both return `401 {"code":"invalid_credentials"}` and both perform an Argon2 verification (against a dummy hash for unknown users) so response timing doesn't leak account existence. Locked accounts return `423 {"code":"account_locked"}` plus a `Retry-After` header (only after a correct-or-incorrect password attempt against an *existing* account; unknown usernames never return 423, so this doesn't enable enumeration beyond what lockout inherently reveals) — the lock itself is not hidden, since hiding it hurts legitimate users more than it helps against attackers who can observe the lockout anyway.

### 11.6 WebSocket authentication

1. Client (holding a valid access token) calls `POST /api/v1/auth/ws-ticket` → receives `{ticket, expires_in: 30}`.
2. Client opens `wss://host/ws?ticket=<ticket>`.
3. Server, **before** `websocket.accept()`, hashes the ticket, runs `UPDATE ws_tickets SET consumed_at=now() WHERE ticket_hash=$1 AND consumed_at IS NULL AND expires_at > now() RETURNING user_id`. Zero rows → reject handshake (close code 4001 after accept, since browsers don't expose HTTP status of a rejected WS upgrade to JS — we accept then immediately close with the application close code so the client can tell auth failure apart from network failure).
4. The single-statement `UPDATE ... RETURNING` makes consumption atomic — a replayed ticket (e.g. leaked from a log) can never open a second socket.

**Why not the JWT in the query string:** URLs are logged by proxies, load balancers, and browser history; a 15-minute bearer credential in a URL is a real leak vector. A 30-second single-use ticket that's useless after first consumption shrinks that risk to near zero. **Why not the first WS message as auth:** it works, but it means the server holds an unauthenticated socket open waiting for a frame (a resource-exhaustion vector needing its own timeout) and every WS handler has to be written to handle "not yet authenticated" state. The ticket approach keeps the gateway's post-accept code path always-authenticated.

**Token expiry during a long-lived WS connection:** the WS connection is bound to the **session** (`sid` = refresh-token family) captured from the access token when the ticket was issued (`ws_tickets.session_family_id`), not to the access token's `exp`. Two mechanisms close it:
1. **Immediate:** logout / logout-all / reuse-detection / account disable write a `session.revoked` event to the outbox (recipients = that user, payload = revoked `family_id`s, or `"*"` for all). The connection manager closes matching sockets with close code **4001 `session_revoked`**. Sockets from the user's *other* sessions (another browser) stay open.
2. **Safety net:** every 5 minutes the connection manager re-validates, in one batched query, that each connected session's family still has a non-revoked, unexpired token and the user is `active`; failures are closed with 4001.

This is simpler and more correct than tracking access-token expiry on the socket, and it gives "log out → all that browser's tabs disconnect" behavior.

## 12. Authorization Model

Authorization is **relationship-based**, derived entirely from `conversation_members` and resource ownership — no global roles in v1 beyond account `status`.

| Action | Rule |
|---|---|
| View a conversation / its history / its members | Caller is an active member (`left_at IS NULL`) |
| Send a message | Caller is an active member |
| Edit / delete a message | Caller is the message's `sender_id` **and** still an active member of the conversation |
| Reply to a message | Caller can send in that conversation **and** `reply_to` belongs to the same conversation |
| Mark read | Caller is an active member |
| Create a DM | Always allowed with any active (non-disabled, non-deleted) user; returns the existing DM if one exists |
| Create a group | Any authenticated user; creator becomes `owner` |
| Add a group member | Caller is an `owner` of that group |
| Remove a group member | Caller is an `owner`, **or** caller is removing themselves (leave) |
| Rename a group | Caller is an `owner` |
| Search messages | Results filtered to conversations where caller is currently an active member |
| Create / edit / cancel scheduled message | Caller is the scheduler (`sender_id`) and an active member *at request time* |
| Scheduled message delivery (worker) | Sender must **still** be an active member at execution time — if not, the scheduled message transitions to `failed` with `last_error='sender_not_member'` and the sender gets a `scheduled_failed` notification |
| Receive a WS event | User id is in the event's precomputed `recipient_user_ids` (see [05](05-database.md) §event_outbox) |
| Send a WS typing event | Caller is an active member of the target conversation (checked against the connection manager's membership cache) |

**Former members and history:** a removed member loses access to the conversation's *future* and *past* history immediately (their `left_at` is set; every query filters on `left_at IS NULL`). This is the simplest correct model and matches Slack's default for private channels; "retain read access to messages sent while you were a member" is a documented possible enhancement, not v1.

**Enforcement points:**
1. A FastAPI dependency `require_active_member(conversation_id)` used by every conversation-scoped route — raises `AuthorizationError` (→403) or `NotFoundError` (→404; for conversations the caller has never been a member of, to avoid leaking existence — see [10](10-errors-logging-security.md) §IDOR).
2. The same check inside application services (defense in depth — services are also invoked by the worker and the WS gateway, which don't go through route dependencies).
3. Queries always carry the membership constraint in the `WHERE` clause/join rather than "fetch then check" where possible, so an authorization bug can't turn into a data leak via a forgotten check.
