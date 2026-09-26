# M02 — Authentication

## Goal
Build our own authentication: registration, login, access JWTs, rotating refresh tokens in an httpOnly cookie with family reuse detection, logout, logout-all, sessions, password change, brute-force lockout and rate limiting, plus the `get_current_user` dependency every later route uses. Includes the frontend login and registration screens with in-memory access tokens and single-flight refresh.

## Dependencies
M01.

## Files/modules expected
- `backend/app/platform/security.py`: the `PasswordHasher` Protocol + `Argon2PasswordHasher` (verify/hash via `asyncio.to_thread`, `needs_rehash`); the `TokenIssuer` Protocol + `JwtTokenIssuer` (HS256, `sub`/`sid`/`jti`/`type`/`iss`/`aud`, previous-secret verification); `generate_opaque_token()` + `sha256_hex()`
- `backend/app/platform/rate_limit.py`: an in-process token bucket, a `rate_limited(key_fn, rate, burst)` dependency factory, trusted-proxy-aware client IP resolution
- `backend/app/modules/identity/domain/`: `RefreshToken` dataclass, auth exceptions (`InvalidCredentials`, `AccountLocked`, `RefreshTokenReused`, `RefreshSuperseded`)
- `backend/app/modules/identity/application/`: `RegisterUser`, `Login`, `RefreshSession`, `Logout`, `LogoutAll`, `ListSessions`, `RevokeSession`, `ChangePassword` services
- `backend/app/modules/identity/infrastructure/refresh_token_repository.py`
- `backend/app/modules/identity/api/`: `routers.py` (`/api/v1/auth/*`), `schemas.py`, `deps.py` (`get_current_user`, `get_current_session_id`, `require_csrf_header`), cookie helpers
- `backend/app/platform/audit.py`: `AuditLogger.record(action, actor, target, metadata)` writing to `audit_logs` inside the caller's UoW
- `frontend/src/api/client.ts`: fetch wrapper that attaches the bearer token and handles 401 by refreshing once (single-flight through `navigator.locks` + a shared promise, with the new token shared over `BroadcastChannel`), retrying once on 409 `refresh_superseded`
- `frontend/src/features/auth/`: login and register pages, an auth context, route guard

## Database changes
- `refresh_tokens` (with the `family_id`, `replaced_by_id` and partial indexes) and `audit_logs`, as in [05](../design/05-database.md).
- The `users` lockout columns (`failed_login_attempts`, `locked_until`), if they weren't already in 0001.

## API changes
All `/api/v1/auth/*` endpoints from [07 §Auth](../design/07-rest-api.md) except `/ws-ticket` (which comes in M06): `register`, `login`, `refresh`, `logout`, `logout-all`, `sessions` (GET, DELETE), `password`. Also a temporary `GET /api/v1/users/me` so the frontend can prove it is authenticated (M03 fills in the rest of `/users`).

## WebSocket changes
None. The `session.revoked` outbox event is added in M09, once the outbox exists. For now logout just revokes the tokens.

## Tests
- Unit: the password policy; JWT encode/verify (expired, wrong `aud`/`iss`/`type`, `alg` confusion, a token signed with the previous secret during rotation); opaque token entropy and length.
- API: every row in the authentication test list in [11 §21.4](../design/11-testing.md), including reuse detection versus the benign concurrent-refresh race, lockout with `423` + `Retry-After` under a fake clock, uniform 401 for unknown user and bad password, and cookie attributes.
- Concurrency: 10 parallel wrong-password attempts → the counter ends at exactly 10 and the account is locked (R-18); two parallel refreshes with the same cookie → one 200 and one 409, with the family intact (R-6).
- Logging: login and refresh flows produce no raw password, token or cookie in any captured log record.
- Audit: `auth.login_failed`, `auth.account_locked` and `auth.refresh_reuse_detected` rows are written.
- Test setup: `COOKIE_SECURE=false` is rejected outside development, so tests keep Secure cookies and use `base_url="https://test"` for the httpx client; otherwise httpx won't send the refresh cookie back.

## Acceptance criteria
- A user can register, log in, reload the page (a silent refresh restores the session), log out, and is then locked out of protected routes.
- In DevTools, the refresh cookie is `HttpOnly; Secure (prod) ; SameSite=Strict; Path=/api/v1/auth`, and the access token is in neither `localStorage` nor `sessionStorage`.
- Replaying an old refresh cookie after the grace window revokes every session in that family.
- Argon2 hashing doesn't block the event loop: in a test, a concurrent `/health/live` request responds in under 50 ms during a login.
