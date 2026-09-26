# M01 — Database Base

## Goal
Set up the persistence foundation: the async SQLAlchemy engine and session, a `UnitOfWork` context manager, Alembic migrations run under a separate owner role, the required extensions, the `users` table, and a test database harness that runs the real migrations. This establishes the transaction discipline (commit and rollback happen only in the UoW) that every later milestone depends on.

## Dependencies
M00.

## Files/modules expected
- `backend/app/platform/db.py`: `create_engine()`, `async_sessionmaker`, `UnitOfWork` (`async with uow:` commits on success, rolls back on exception; `uow.savepoint()` async context manager wrapping `begin_nested()`), a SQLAlchemy `MetaData(naming_convention=…)` so constraint names are deterministic
- `backend/app/platform/models_base.py`: declarative `Base`, a `TimestampMixin` (`created_at`, `updated_at` with a server default)
- `backend/app/modules/identity/infrastructure/models.py`: the `User` ORM model
- `backend/app/modules/identity/domain/user.py`: the `User` dataclass and `UserStatus` enum
- `backend/app/modules/identity/infrastructure/user_repository.py`: `get_by_id`, `get_by_username_or_email`, `add`
- `backend/alembic.ini`, `backend/alembic/env.py` (async, reads `DATABASE_OWNER_URL`)
- `backend/alembic/versions/0001_extensions_and_users.py`
- `db/init/01-roles.sh` (creates the `chat_app` login role from `CHAT_APP_PASSWORD`; `chat_owner` is the image's `POSTGRES_USER`). Grants, default privileges and `idle_in_transaction_session_timeout` are in migration 0001, per [12 §22.5](../design/12-local-dev-docker.md)
- `docker-compose.yml`: the `migrate` one-shot service; `api` depends on it
- `backend/tests/conftest.py`: the server from `TEST_DATABASE_ADMIN_URL` or else a Postgres testcontainer per session, the `chat_app` role ensured, a database per xdist worker, `alembic upgrade head` as the owner, the transactional-rollback session fixture connecting as `chat_app`, the `real_commits` marker and truncate teardown ([11 §21.3](../design/11-testing.md))
- `backend/tests/factories.py`: `make_user`

## Database changes
- Extensions: `pgcrypto`, `citext`, `pg_trgm`.
- The `users` table exactly as in [05 §users](../design/05-database.md), including the partial unique indexes on `username`/`email` (`WHERE deleted_at IS NULL`) and the trigram GIN indexes.
- Roles and grants as in [12 §22.5](../design/12-local-dev-docker.md): the role is created by the init script, and the grants, default privileges and role timeout by migration 0001.

## API changes
- `/health/ready` now runs `SELECT 1` through the pool and returns 503 on failure.

## WebSocket changes
None.

## Tests
- Integration: the UoW commits on a clean exit; it rolls back on an exception and re-raises; a savepoint rollback leaves the outer transaction usable.
- Integration: a `citext` username is unique case-insensitively; a soft-deleted user frees the username (the partial index).
- Integration: the Alembic `upgrade head` → `downgrade base` → `upgrade head` round trip.
- Integration: the app role can't run DDL (`CREATE TABLE` fails with permission denied).
- Integration: the constraint names produced by the naming convention match the expected names (the start of the "constraint registry" test that later milestones extend).

## Acceptance criteria
- `docker compose run --rm migrate` applies the schema; `api` connects as `chat_app`.
- `/health/ready` returns 503 when `db` is stopped and 200 again after it restarts, without restarting the API.
- The test suite runs against real Postgres with isolation per test; `pytest -n auto` passes.
- No call to `session.commit()` exists outside `platform/db.py` (a grep check in CI).
