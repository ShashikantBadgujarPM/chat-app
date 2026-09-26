# M04 — Conversations and Membership

## Goal
Build direct (1:1) and group conversations: race-safe DM creation, group creation, adding and removing members (owner-only, plus leaving), renaming, and listing the caller's conversations. It also delivers the reusable authorization dependency `require_active_member` and the policy matrix test that every later conversation-scoped feature relies on.

## Dependencies
M02, M03.

## Files/modules expected
- `backend/app/modules/conversations/domain/`: `Conversation` and `ConversationMember` dataclasses; `ConversationType` and `MemberRole` enums; `direct_key(a, b)`; invariant exceptions (`CannotRemoveLastOwner`, `NotAGroup`, `CannotDmSelf`, `MemberLimitExceeded`)
- `backend/app/modules/conversations/application/conversation_service.py`: `get_or_create_direct`, `create_group`, `add_members`, `remove_member`, `rename`, `list_for_user`, `get`, `list_members`
- `backend/app/modules/conversations/infrastructure/`: ORM models + repository (`insert_direct_on_conflict`, `lock_owner_rows`, `upsert_member`)
- `backend/app/modules/conversations/api/`: router, schemas, `deps.py` with `require_active_member(conversation_id)` (non-member → 404) and `require_owner`
- `backend/app/platform/decorators.py` (optional): `@audit_logged("conversation.member_added")`, if it reads more clearly than an explicit call
- `frontend/src/features/conversations/`: sidebar conversation list, "new DM" and "new group" dialogs (reusing the M03 user search), a member management panel

## Database changes
- `conversations` (with `direct_key` and its partial unique index `WHERE type='direct'`, `last_message_seq` defaulting to 0) and `conversation_members` (composite PK, partial indexes on `user_id` and `conversation_id` `WHERE left_at IS NULL`, `last_read_seq`, `notifications_muted`), as in [05](../design/05-database.md).

## API changes
From [07 §Conversations](../design/07-rest-api.md): `GET /conversations`, `POST /conversations/direct`, `POST /conversations/groups`, `GET /conversations/{id}`, `PATCH /conversations/{id}`, `GET /conversations/{id}/members`, `POST /conversations/{id}/members`, `DELETE /conversations/{id}/members/{user_id}`, `PATCH /conversations/{id}/membership`.
- `ConversationSummary` fields that depend on messages (`last_message`, `unread_count`, `unread_mention_count`) return `null`/0 until M05 and M07.
- `PUT /read-cursor` comes in M07.

## WebSocket changes
None yet. The `conversation.*` events are wired in M06; the service already *calls* `EventPublisher.publish(...)` through a no-op publisher injected here, so M06 only has to swap in the implementation.

## Tests
- Integration (R-2): 20 concurrent `get_or_create_direct(A, B)` / `(B, A)` calls → exactly 1 conversation, and every caller gets the same id.
- Integration (R-12): two owners removing each other concurrently → exactly one succeeds and one gets 409; at least one owner remains.
- Integration (R-14): concurrent adds of the same user → one active membership. Re-adding a former member clears `left_at` and sets `last_read_seq` to the current `last_message_seq`.
- API: the full **authorization matrix** for the conversation endpoints (non-member → 404, member non-owner → 403 on owner-only actions, former member → 404, owner → allowed, self-leave allowed).
- API: DM with yourself → 422; DM with a missing or deleted user → 404; a repeat DM → 200 with the same id; a group title that's empty or over 100 characters → 422; adding past 100 members → 422.
- Unit: `direct_key` is order-independent; the invariants.

## Acceptance criteria
- Two users can start a DM from either side and always land in the same conversation.
- A group owner can create a group, add and remove members, and rename it; a non-owner can't; any member can leave. Removing the last owner is refused.
- The conversation list shows only the caller's active memberships, ordered by most recent activity (creation time for now).
- The authorization matrix test exists and is parametrized from a single policy table in the test module.
