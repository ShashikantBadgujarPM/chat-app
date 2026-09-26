# 13. REST API Contract

## 13.1 Conventions

- **Base path:** `/api/v1`. Breaking changes → `/api/v2`, with v1 kept alive during migration. Additive changes (new optional fields, new endpoints) do not bump the version.
- **Auth:** `Authorization: Bearer <access_token>` on everything except `register`, `login`, `refresh`, `logout`, and `/health`. "Auth" column below: `public`, `bearer`, `cookie` (refresh cookie + `X-Requested-With: chat-app`).
- **Content type:** `application/json; charset=utf-8`. Field names are `snake_case` (Pydantic default). The frontend maps them once in its API layer.
- **IDs:** UUID strings. **Timestamps:** RFC 3339 in UTC with a `Z` suffix (`2026-10-01T12:30:00Z`). The server never returns local times; clients format for display.
- **Pagination:**
  - Messages use **seq keyset**: `?before_seq=` / `?after_seq=` and `limit` (default 50, max 100). Response: `{items, has_more}`.
  - Other lists use an **opaque cursor**: `?cursor=` and `limit`. Response: `{items, next_cursor}`, where `next_cursor` is `null` at the end. The cursor is base64 JSON of the last `(sort_key, id)`, so offset pagination drift can't happen.
- **Idempotency:**
  - Message send and scheduled-message create take `client_message_id` in the body (a UUID the client generates once per compose action). A retry with the same id returns **200** with the original resource instead of 201.
- **Correlation:** clients may send `X-Request-ID`. The server echoes it or generates one, and includes it in every response and log line.

### Error envelope (every non-2xx)

```json
{
  "error": {
    "code": "not_a_member",
    "message": "You are not a member of this conversation.",
    "details": null,
    "request_id": "b1f0..."
  }
}
```

For `422`, `details` holds a list of `{field, issue}`, derived from Pydantic errors but reshaped so internal model structure isn't leaked. `code` is a stable, documented machine string that the frontend can switch on. `message` is for humans and may change.

| Status | Meaning here |
|---|---|
| 400 | Malformed request the schema can't express (e.g. both `before_seq` and `after_seq`) |
| 401 | Missing, invalid or expired access token; bad credentials; revoked refresh token |
| 403 | Authenticated, and the resource's existence is known to the caller, but the action is not allowed (e.g. a non-owner removing a member) |
| 404 | Not found **or not visible to the caller** (a non-member asking for a conversation gets 404, not 403, to avoid leaking existence) |
| 409 | State conflict (scheduled message no longer pending; editing a deleted message; refresh superseded) |
| 422 | Validation failed |
| 423 | Account temporarily locked (brute-force protection), with `Retry-After` |
| 429 | Rate limited, with `Retry-After` |
| 500 | Unhandled. Generic message only; details go to logs under `request_id` |

## 13.2 Resource boundaries (and why)

- **`/auth`** is separate from `/users`: it covers credentials and sessions, not user data. Its routes carry different auth mechanics (cookie vs bearer) and different rate limits.
- **`/users/me`** is the caller's own profile (mutable). **`/users/{id}`** and **`/users?q=`** are other users' public projections (read-only). This prevents "PATCH another user's profile" by construction.
- **Messages are nested under conversations for list and create** (`/conversations/{id}/messages`), because a message has no meaning outside its conversation, and nesting makes the membership check apply to the path. **Messages are top-level for item operations** (`/messages/{id}` for PATCH/DELETE), because the message id alone identifies it and forcing the client to also supply the conversation id adds a way to be inconsistent without adding safety. The server derives the conversation from the message.
- **Read state is a sub-resource of membership**, not of messages: `PUT /conversations/{id}/read-cursor`. It sets one cursor value (idempotent PUT) instead of POSTing per-message read receipts. This follows from [ADR-003](../README.md#adr-003-read-cursors-instead-of-per-message-read-rows).
- **Search is its own resource** (`/search/messages`): it crosses conversations and has a different result shape (snippets, highlights).
- **`/scheduled-messages`** is top-level, because the main view is "all my scheduled messages across conversations". The target conversation is a field in the body, validated for membership.

## 13.3 Endpoints

### Health

| Method | Path | Auth | Response |
|---|---|---|---|
| GET | `/health/live` | public | `200 {"status":"ok"}`. The process is up. |
| GET | `/health/ready` | public | `200` if the DB is reachable and the outbox listener is connected, otherwise `503`. Used by the compose healthcheck. |

### Auth — `/api/v1/auth`

| Method | Path | Auth | Request | Success | Errors |
|---|---|---|---|---|---|
| POST | `/register` | public | `{username, email, display_name, password, timezone?}` | `201 UserMe` | 409 `username_taken` / `email_taken`; 422 `weak_password`; 429 |
| POST | `/login` | public | `{username_or_email, password}` | `200 {access_token, token_type:"bearer", expires_in, user: UserMe}` + `Set-Cookie: rt` | 401 `invalid_credentials`; 423 `account_locked`; 429 |
| POST | `/refresh` | cookie | — | `200 {access_token, token_type, expires_in}` + rotated `Set-Cookie: rt` | 401 `invalid_refresh_token` / `refresh_token_reused` (the cookie is cleared); 409 `refresh_superseded` (retry once) |
| POST | `/logout` | cookie | — | `204`, cookie cleared. Idempotent: 204 even if already revoked | — |
| POST | `/logout-all` | bearer | — | `204`. Revokes every session of the caller and publishes `session.revoked` | 401 |
| GET | `/sessions` | bearer | — | `200 {items: [Session]}` | 401 |
| DELETE | `/sessions/{family_id}` | bearer | — | `204`. Revokes one session | 404 if the session isn't the caller's |
| POST | `/ws-ticket` | bearer | — | `201 {ticket, expires_in: 30}` | 401; 429 (10/min/user) |
| POST | `/password` | bearer | `{current_password, new_password}` | `204`. Revokes all *other* sessions | 401 `invalid_credentials`; 422 `weak_password` |

`Session = {family_id, created_at, last_used_at, user_agent, ip_address, current: bool}`. Derived per `family_id` from `refresh_tokens`: `created_at` is the first token's `issued_at`, `last_used_at` the latest token's `issued_at` (each refresh issues a new token), and `current` means the family matches the caller's `sid` claim.

### Users — `/api/v1/users`

| Method | Path | Auth | Request | Success | Errors |
|---|---|---|---|---|---|
| GET | `/me` | bearer | — | `200 UserMe` | 401 |
| PATCH | `/me` | bearer | `{display_name?, timezone?}` (partial update; unknown fields → 422) | `200 UserMe` | 422 `invalid_timezone` (validated against `zoneinfo.available_timezones()`) |
| DELETE | `/me` | bearer | `{password}` | `204`. Soft delete plus revoke all sessions | 401 |
| GET | `/` `?q=&limit=&cursor=` | bearer | `q` has at least 2 characters | `200 {items: [UserPublic], next_cursor}`. Trigram similarity on username and display_name; excludes the caller, deleted and disabled users | 422 `query_too_short` |
| GET | `/{user_id}` | bearer | — | `200 UserPublic` | 404 |
| GET | `/presence` `?ids=a,b,c` | bearer | up to 100 ids | `200 {items: [{user_id, status, last_seen_at}]}` | 422 |

```
UserMe     = {id, username, email, display_name, timezone, created_at}
UserPublic = {id, username, display_name, presence: {status, last_seen_at}}
```

Presence is exposed over REST as well so the UI can render correctly before the WS connects and after a reconnect. The live changes then arrive over WS.

### Conversations — `/api/v1/conversations`

| Method | Path | Auth / Authz | Request | Success | Errors |
|---|---|---|---|---|---|
| GET | `/` `?cursor=&limit=` | bearer | — | `200 {items: [ConversationSummary], next_cursor}`, sorted by `last_activity_at DESC` | — |
| POST | `/direct` | bearer | `{user_id}` | `201 Conversation` if created, **`200`** if it already existed (idempotent) | 404 `user_not_found`; 422 `cannot_dm_self` |
| POST | `/groups` | bearer | `{title, member_ids: [uuid] (0..99)}` | `201 Conversation`; the caller is the owner | 422 (title 1–100 chars); 404 `user_not_found` (lists the ids) |
| GET | `/{id}` | member | — | `200 Conversation` | 404 |
| PATCH | `/{id}` | owner (group) | `{title}` | `200 Conversation` | 403 `not_owner`; 422 `not_a_group` |
| GET | `/{id}/members` | member | — | `200 {items: [Member]}` | 404 |
| POST | `/{id}/members` | owner (group) | `{user_ids: [uuid] (1..50)}` | `200 {added: [Member], already_members: [uuid]}` | 403; 404; 422 `not_a_group`; 422 `member_limit_exceeded` (group cap of 100) |
| DELETE | `/{id}/members/{user_id}` | owner, or self (leave) | — | `204` | 403; 404; 409 `cannot_remove_last_owner` |
| PUT | `/{id}/read-cursor` | member | `{last_read_seq}` | `200 {last_read_seq, unread_count}` | 404; 422 if > `last_message_seq` |
| PATCH | `/{id}/membership` | member | `{notifications_muted}` | `200 Member` | 404 |

```
Conversation        = {id, type: "direct"|"group", title|null, created_at,
                       last_message_seq, members_preview: [UserPublic] (up to 5), member_count,
                       my_role: "owner"|"member"}
ConversationSummary = Conversation + {last_message: MessageBrief|null, unread_count,
                       unread_mention_count, last_activity_at, my_last_read_seq}
Member              = {user: UserPublic, role, joined_at}
```

- **Read cursor semantics:** the server stores `GREATEST(current, requested)`, so the cursor never moves backward. An out-of-order request from a stale tab therefore can't mark messages unread again. Moving the cursor publishes `conversation.read` to the *caller's own* other connections (multi-tab badge sync). For read receipts, it also publishes `receipt.updated` to the other members, but only for direct conversations and groups under 20 members, which keeps fan-out bounded.
- **Direct conversation title:** for `type=direct` the response has `title: null`, and the client renders the other member's name. The server does not store a per-viewer title.

### Messages

| Method | Path | Authz | Request | Success | Errors |
|---|---|---|---|---|---|
| GET | `/conversations/{id}/messages` `?before_seq=&after_seq=&limit=` | member | at most one of before/after; neither = latest page | `200 {items: [Message] (seq DESC for before/latest, ASC for after), has_more}` | 400 both cursors; 404 |
| GET | `/conversations/{id}/messages/around/{seq}` `?limit=` | member | jump-to-message (search results, replies) | `200 {items, has_more_before, has_more_after}` | 404 |
| POST | `/conversations/{id}/messages` | member | `{client_message_id, body (1..4000 chars, trimmed), reply_to_id?}` | `201 Message`, or `200 Message` on idempotent replay | 404; 422 `reply_not_in_conversation`; 422 `body_empty` / `body_too_long`; 429 (per-user send limit) |
| GET | `/messages/{id}` | member of its conversation | — | `200 Message` | 404 |
| PATCH | `/messages/{id}` | sender and active member | `{body}` | `200 Message` (with `edited_at` set) | 403 `not_sender`; 404; 409 `message_deleted` |
| DELETE | `/messages/{id}` | sender and active member | — | `204`. Soft delete: body nulled, `deleted_at` set, mentions removed | 403; 404. Idempotent: repeating it returns 204 |

```
Message      = {id, conversation_id, seq, sender: UserPublic|null, body|null,
                reply_to: MessageBrief|null, mentions: [uuid],
                created_at, edited_at|null, deleted_at|null, scheduled_message_id|null,
                client_message_id}
MessageBrief = {id, seq, sender_id, body_preview (up to 120 chars, "" if deleted), deleted: bool}
```

- **Mentions** are parsed on the server from the body, using the `@username` pattern `(?<![\w@])@([A-Za-z0-9_]{3,32})`. They are resolved against the conversation's **active members** only; `@someone` who isn't a member is left as plain text. The client is not trusted to send the mention list. On edit, mentions are re-parsed, and only *newly added* mentions create notifications, so an edit doesn't re-notify.
- **Deleted messages** remain in history as tombstones (`body: null, deleted_at` set). The UI shows "This message was deleted." This keeps `seq` continuous and keeps reply references meaningful.

### Search

| Method | Path | Authz | Request | Success | Errors |
|---|---|---|---|---|---|
| GET | `/search/messages` `?q=&conversation_id=&cursor=&limit=` | bearer; results limited to active memberships | `q` 2–200 chars | `200 {items: [{message: Message, conversation_id, snippet_html, rank}], next_cursor}` | 422; 404 if the `conversation_id` filter isn't visible |

- **Query parsing and ordering:** the server uses `websearch_to_tsquery('english', q)`, which safely parses user input (quotes, `-exclusion`, `or`) and never raises on odd input. Results are ordered by `ts_rank` and then `created_at DESC`.
- **Snippets:** `snippet_html` comes from `ts_headline`, and the server HTML-escapes it except for `<mark>` tags. The frontend renders it with a sanitizer; see [10](10-errors-logging-security.md) §XSS.

### Scheduled messages — `/api/v1/scheduled-messages`

| Method | Path | Authz | Request | Success | Errors |
|---|---|---|---|---|---|
| POST | `/` | bearer and member of the target | `{client_message_id, conversation_id, body, reply_to_id?, scheduled_at, timezone?}` | `201 ScheduledMessage` (200 on idempotent replay) | 404; 422 `scheduled_in_past` (must be at least 30 s from now), `scheduled_too_far` (at most 1 year), `invalid_timezone`; 429 `too_many_pending` (100 pending per user) |
| GET | `/` `?status=pending|sent|failed|cancelled&conversation_id=&cursor=&limit=` | own only | — | `200 {items: [ScheduledMessage], next_cursor}`, ordered by `scheduled_at` | — |
| GET | `/{id}` | own only | — | `200 ScheduledMessage` | 404 |
| PATCH | `/{id}` | own; `status=pending` | `{body?, scheduled_at?, timezone?, reply_to_id?}` | `200 ScheduledMessage` | 404; **409 `not_pending`** (already sent, cancelled or failed — including when the worker is sending it right now); 422 as for create |
| POST | `/{id}/cancel` | own; `status=pending` | — | `200 ScheduledMessage` (status `cancelled`) | 404; 409 `not_pending`. Idempotent: cancelling an already-cancelled message returns 200 |
| POST | `/{id}/retry` | own; `status=failed` | `{scheduled_at?}` (default: now + 30 s) | `200 ScheduledMessage` (back to `pending`, attempts reset) | 409 `not_failed`; 422 |

```
ScheduledMessage = {id, conversation_id, body, reply_to_id|null,
                    scheduled_at (UTC, Z), timezone, status, attempts,
                    last_error|null, sent_message_id|null (derived by joining messages.scheduled_message_id), sent_at|null,
                    created_at, updated_at, cancelled_at|null}
```

- **Time zones on input:** `scheduled_at` must be an RFC 3339 timestamp **with an offset** (`2026-10-01T18:00:00+05:30`). A naive timestamp is rejected with 422 `naive_datetime` rather than guessed at. `timezone` (IANA name, defaulting to the user's profile timezone) is stored only for display and for future recurrence rules. The instant used is always the offset-qualified `scheduled_at`, converted to UTC. This removes any server-side ambiguity around DST.
- **Cancel is a POST action, not DELETE:** the row is kept (status `cancelled`) for history and audit. DELETE would suggest the row is gone.

### Notifications — `/api/v1/notifications`

| Method | Path | Auth | Request | Success |
|---|---|---|---|---|
| GET | `/` `?unread_only=&cursor=&limit=` | bearer | — | `200 {items: [Notification], next_cursor, unread_count}` |
| POST | `/{id}/read` | bearer (owner) | — | `204` (idempotent); 404 if not the caller's |
| POST | `/read-all` | bearer | `{before?: timestamp}` | `204` |

`Notification = {id, type, conversation_id|null, message_id|null, payload, read_at|null, created_at}`

### Sync (REST fallback used by the WS reconnect protocol)

| Method | Path | Auth | Request | Success |
|---|---|---|---|---|
| GET | `/sync/events` `?after_event_id=&limit=` | bearer | `limit` at most 500 | `200 {events: [WSEvent], has_more, reset_required: bool}`. The same envelope as WS events; see [08](08-websocket.md) §Missed-event sync |

## 13.4 Rate limits (v1 defaults, all configurable)

| Scope | Limit |
|---|---|
| `POST /auth/login` | 10/min per IP, plus the per-account lockout |
| `POST /auth/register` | 5/hour per IP |
| `POST /auth/refresh` | 30/min per IP |
| `POST /auth/ws-ticket` | 10/min per user |
| Message send (REST) | 30/10 s per user (burst-friendly token bucket) |
| WS client events (typing, read) | 20/s per connection, beyond which extras are dropped, and a warning is logged once per minute |
| Everything else (authenticated) | 300/min per user |
