# M03 — Users: Profile and Search

## Goal
Let users view and update their own profile (display name, time zone), view other users' public profiles, search for users with fuzzy matching, and delete their own account (soft delete). This establishes the `UserMe`/`UserPublic` projections and the cursor pagination helper that later list endpoints reuse.

## Dependencies
M02.

## Files/modules expected
- `backend/app/platform/pagination.py`: opaque cursor encode/decode (base64 JSON of `(sort_key, id)`), a `Page[T]` generic Pydantic model
- `backend/app/modules/identity/application/profile_service.py`: `get_me`, `update_me`, `delete_me`, `search_users`, `get_public_profile`
- `backend/app/modules/identity/api/users_router.py` (`/api/v1/users`)
- `backend/app/modules/identity/api/schemas.py`: `UserMe`, `UserPublic`, `UpdateMeRequest` (every field optional, `extra="forbid"`, timezone validated against `zoneinfo.available_timezones()`)
- `frontend/src/features/users/`: profile page, a user-search combobox component (debounced) that M04 reuses for "start a DM" and "add a member"

## Database changes
None beyond M01. This milestone uses the existing trigram indexes.

## API changes
From [07 §Users](../design/07-rest-api.md): `GET /users/me`, `PATCH /users/me`, `DELETE /users/me`, `GET /users?q=`, `GET /users/{id}`. `GET /users/presence` is deferred to M08. `UserPublic.presence` returns `{status: "offline", last_seen_at: null}` until M08.

## WebSocket changes
None.

## Tests
- API: a PATCH changes only the fields provided; an unknown field → 422; an invalid timezone → 422 `invalid_timezone`.
- API (queries must use `username::text` to hit the trigram index; see [05 §users](../design/05-database.md)): search matches a partial username or display name (`"shas"` finds `shashikant`); a query under 2 characters → 422; the results exclude the caller and deleted or disabled users; pagination is stable across pages, with no duplicates or omissions, when a new user registers between page fetches.
- API: `DELETE /users/me` with the wrong password → 401; with the right one → 204, every session is revoked, login fails, and the username becomes available again.
- Unit: cursor encode/decode round trip; a tampered cursor → 400, not 500.

## Acceptance criteria
- A user can change their display name and time zone and see the change after reloading.
- A user can find another user by typing part of their name and open their public profile.
- The search query plan uses the trigram GIN index (`EXPLAIN` checked once manually and noted in the PR).
