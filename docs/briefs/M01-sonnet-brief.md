# M01 — Implementation Brief for the Sonnet Agent

This brief supplements [docs/milestones/M01-database-base.md](../milestones/M01-database-base.md); it does not replace it. It records what already exists (so M00 isn't redone), the decisions already made (so you don't make them again), and the concrete details the milestone file leaves to the implementer. If anything here seems to contradict the design docs, stop and record it in [docs/decisions/open-questions.md](../decisions/open-questions.md) before continuing.

## 1. Prompt

```
You are implementing milestone M01 (Database Base) of the chat-app project.

Read, in order:
  docs/README.md
  docs/design/03-backend-architecture.md
  docs/milestones/M01-database-base.md
  docs/briefs/M01-sonnet-brief.md            (this file)
  docs/decisions/open-questions.md           (Q-004 to Q-006 apply to M01)
  docs/design/05-database.md  §users, §9.4 extensions
  docs/design/10-errors-logging-security.md  §18 (UoW, constraint names), §19
  docs/design/11-testing.md   §21.1, §21.3
  docs/design/12-local-dev-docker.md  §22.1, §22.2, §22.4, §22.5, §22.6
  docs/design/15-sonnet-strategy.md   §26.4, §26.5

Then read the existing backend code (backend/app, backend/tests) before changing anything.

Rules:
- The design docs are the spec. Implement exactly what M01 defines, nothing from M02 or later.
- M00 is complete and reviewed. Extend its modules; do not rewrite or restructure them.
- If the spec is ambiguous or contradictory: stop, add an OPEN entry to
  docs/decisions/open-questions.md with your proposed resolution, and ask.
- Follow the non-negotiables in docs/design/15-sonnet-strategy.md §26.4.
- Commit order: config + db.py + UoW (unit tests) → migration + roles script
  → model + repository (integration tests) → compose/Docker → health check → CI.
  The full test suite passes before each step.
- Finish by reporting every M01 acceptance criterion and every item in §7 of this brief
  as PASS/FAIL with evidence (test name or command output).
```

## 2. What already exists (M00): reuse it, don't redo it

| Piece | Where | How M01 uses it |
|---|---|---|
| `Settings` (frozen, fail-fast validators, `safe_summary()`) | `app/config.py` | Add the database fields (§4.1). Never put URLs in `safe_summary()` |
| `create_app(settings)` + lifespan skeleton | `app/main.py` | Create the engine in the lifespan, register the readiness check, dispose on shutdown |
| Readiness registry | `app/platform/health.py`: `register_readiness_check(app, name, check)` | Register `"database"`. Don't change the endpoint or its envelope |
| Error hierarchy + envelope | `app/platform/errors.py` | Use `AppError` subclasses; don't add new envelope shapes |
| Logging: `logger.info(msg, extra={"event": ...})`, `bind_log_context` | `app/platform/logging.py` | New log lines use stable `event` names (§4.6) |
| Test fixtures: autouse env isolation, `make_settings`, `app`, `client` | `tests/conftest.py` | Extend them. Keep the autouse isolation fixture |
| Dockerfile (`dev` and `runtime` targets, non-root) | `backend/Dockerfile` | Both targets must now also copy `alembic/` and `alembic.ini` |
| Compose (`db`, `api`; `web` in the override) | `docker-compose*.yml` | Add `migrate`, the init script mount, and the new environment variables |
| CI | `.github/workflows/ci.yml` | Add the commit grep check (§4.8) |

Tooling commands: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict app`, `uv run pytest -n auto`, all from `backend/`.

## 3. Decisions already made: don't reopen them

- **Q-004:** `db/init/01-roles.sh` creates only the `chat_app` login role, from `CHAT_APP_PASSWORD`. Migration 0001 applies the grants, the default privileges and `ALTER ROLE chat_app SET idle_in_transaction_session_timeout = '10s'`.
- **Q-005:** the test server comes from `TEST_DATABASE_ADMIN_URL` if it is set, otherwise from a testcontainer. Application sessions connect as `chat_app`.
- **Q-006:** the username trigram index is `GIN ((username::text) gin_trgm_ops)`.
- **Health** stays at `/health/*`. Every non-2xx response uses the `{"error": {...}}` envelope (Q-001, Q-002).

## 4. Implementation details the milestone leaves open (decided here)

### 4.1 Settings (`app/config.py`)
- `database_url: SecretStr` is **required**, and must start with `postgresql+asyncpg://` (validator).
- `db_pool_size: int = 10` (≥1) and `db_max_overflow: int = 5` (≥0), per 12 §22.4.
- Do **not** add `DATABASE_OWNER_URL` to `Settings`. Only Alembic uses it, and it reads it directly (§4.4), so the API process never holds the owner credential.
- `tests/conftest.py` must `setdefault` a dummy `DATABASE_URL` (such as `postgresql+asyncpg://unused:unused@127.0.0.1:1/unused`) next to `JWT_SECRET`, because `app.main` builds its app at import time. Engine creation is lazy, so nothing connects at import.
- Extend the "secret not in repr" test to cover `database_url`.

### 4.2 `app/platform/db.py`
- `NAMING_CONVENTION`, exactly as follows, since later milestones match constraints by these names:
  `pk_%(table_name)s`, `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`, `uq_%(table_name)s_%(column_0_N_name)s`, `ck_%(table_name)s_%(constraint_name)s`, `ix_%(table_name)s_%(column_0_N_name)s`.
- `create_engine(settings) -> AsyncEngine`: `create_async_engine(..., pool_size, max_overflow, pool_pre_ping=True)`. `pool_pre_ping` is what lets `/health/ready` return 200 again after a DB restart without restarting the API.
- `create_session_factory(engine) -> async_sessionmaker[AsyncSession]` with `expire_on_commit=False`.
- `UnitOfWork(session_factory)`:
  - `async with uow:` opens a session. On a clean exit it commits; on an exception it rolls back and re-raises; the session is always closed.
  - `uow.session` raises `RuntimeError` if the UoW hasn't been entered. Entering an already-entered UoW raises `RuntimeError`.
  - `uow.savepoint()` is an async context manager around `session.begin_nested()`. It releases on success and rolls back to the savepoint on an exception, which propagates.
  - This is the **only** place in `app/` that calls `commit()` or `rollback()`.
- `async def check_database(engine) -> None`: `SELECT 1` through `engine.connect()`, inside `asyncio.timeout(READINESS_DB_TIMEOUT_SECONDS)`, where the module constant is `2.0`. It isn't a new environment variable, because 12 §22.4 doesn't list one. It raises on failure, and the registry turns that into 503.
- **Learning note (15 §26.6):** the human intends to write `UnitOfWork` personally. If `app/platform/db.py` already contains a `UnitOfWork` when you start, review it against this section and extend its tests instead of replacing it.

### 4.3 `app/platform/models_base.py`
- `class Base(DeclarativeBase)` with `metadata = MetaData(naming_convention=NAMING_CONVENTION)`.
- `TimestampMixin`: `created_at` and `updated_at` are `timestamptz`, `NOT NULL`, with `server_default=func.now()`. `updated_at` also gets `onupdate=func.now()`. Add a docstring convention: raw `text()` UPDATEs must set `updated_at = now()` themselves, because `onupdate` doesn't apply to raw SQL.

### 4.4 Alembic
- `alembic.ini`: `script_location = %(here)s/alembic`, `prepend_sys_path = .`, no `sqlalchemy.url`, and the file template `%%(rev)s_%%(slug)s`.
- `alembic/env.py` is async (psycopg 3 supports asyncio, so use `create_async_engine` with the `postgresql+psycopg://` URL and `NullPool`). URL resolution:
  1. `config.get_main_option("sqlalchemy.url")`, which the test harness sets;
  2. otherwise `os.environ["DATABASE_OWNER_URL"]`, with a clear error if it's missing.
  Don't import `get_settings()`, because the migrate container has no `JWT_SECRET`.
- `fileConfig(...)` runs only when `config.attributes.get("configure_logger", True)`, and with `disable_existing_loggers=False`. The test harness passes `False`.
- `target_metadata = Base.metadata`. Import `app.modules.identity.infrastructure.models` in `env.py` so autogenerate sees the model.
- **`0001_extensions_and_users.py` is written by hand** (autogenerate can't produce extensions, grants, or partial or expression indexes). Contents:
  1. `CREATE EXTENSION IF NOT EXISTS` for `pgcrypto`, `citext` and `pg_trgm`.
  2. A guard that raises a clear error if the `chat_app` role doesn't exist, citing 12 §22.5.
  3. The grants from 12 §22.5, using `EXECUTE format('GRANT CONNECT ON DATABASE %I TO chat_app', current_database())` for the database-name part. Use `ALTER DEFAULT PRIVILEGES FOR ROLE chat_owner IN SCHEMA public …` for tables and sequences. Then `ALTER ROLE chat_app SET idle_in_transaction_session_timeout = '10s'`.
  4. The `users` table exactly as in 05 §users: `id UUID DEFAULT gen_random_uuid()`, `citext` username and email, `CHECK` on status named `ck_users_status`, defaults, timestamps.
  5. Indexes with explicit names, because M02 matches unique violations by these names:
     - `uq_users_username_active`: `UNIQUE (username) WHERE deleted_at IS NULL`
     - `uq_users_email_active`: `UNIQUE (email) WHERE deleted_at IS NULL`
     - `ix_users_username_trgm`: `GIN ((username::text) gin_trgm_ops)`
     - `ix_users_display_name_trgm`: `GIN (display_name gin_trgm_ops)`
  6. `downgrade()` drops the table, revokes the grants and default privileges, and drops the extensions. It does **not** reset the role setting, because the role is cluster-wide and other databases (such as other test databases) rely on it.

### 4.5 Identity module
- `domain/user.py`: `class UserStatus(StrEnum)` with `ACTIVE`/`DISABLED`, and a `@dataclass(frozen=True, slots=True) class User` holding every column. `password_hash` is declared with `field(repr=False)`, because M02 needs it for login but it must never show up in a repr or a log line. The domain layer has no SQLAlchemy imports.
- `infrastructure/models.py`: an ORM `UserModel(Base, TimestampMixin)` that mirrors the migration, including the index definitions (so `alembic check` / autogenerate shows no diff).
- `infrastructure/user_repository.py`: `UserRepository(session)` with `get_by_id`, `get_by_username_or_email(identifier)` (a case-insensitive match through `citext`, excluding soft-deleted users), and `add(...) -> User`. `add` flushes, so the DB assigns the id and defaults, and never commits. It returns domain `User` objects, never ORM instances.

### 4.6 Lifespan, health, logging
- Lifespan: create the engine and session factory and put them on `app.state`. Call `register_readiness_check(app, "database", partial(check_database, engine))`. The `finally` block calls `await engine.dispose()`.
- **Startup must not fail when the DB is down.** `/health/live` must stay 200 and `/health/ready` must return 503.
- The M00 test `test_ready_returns_200_with_no_checks_registered` no longer applies. Replace it with an integration test in which a reachable DB gives 200 with `{"checks": {"database": "ok"}}`, and keep the fake-check API tests.
- Log events: `db.engine_created` (INFO: pool size and max overflow, never the URL) and `db.engine_disposed`. The readiness failure is already logged by `health.check_failed`.

### 4.7 Compose, Docker and the environment
- `db`: mount `./db/init:/docker-entrypoint-initdb.d:ro`, and pass `CHAT_APP_PASSWORD`. `01-roles.sh` is `#!/bin/sh`, uses `set -eu`, and runs `psql -v ON_ERROR_STOP=1 -v app_password="$CHAT_APP_PASSWORD"` with `CREATE ROLE chat_app LOGIN PASSWORD :'app_password';`. It must have LF line endings (`.gitattributes` already enforces this).
- `migrate`: runtime image, `command: alembic upgrade head`, `restart: "no"`, and `depends_on: db: service_healthy`. Its environment has **only** `DATABASE_OWNER_URL` (`postgresql+psycopg://chat_owner:${POSTGRES_OWNER_PASSWORD}@db:5432/chat`).
- `api`: `depends_on: migrate: service_completed_successfully`, and `DATABASE_URL=postgresql+asyncpg://chat_app:${CHAT_APP_PASSWORD}@db:5432/chat`. The api never receives the owner URL.
- Dev override: `migrate` bind-mounts `./backend/alembic:/app/alembic`, so `docker compose run --rm migrate alembic revision …` writes to the host.
- `.env.example`: add `CHAT_APP_PASSWORD=dev-only-app-password`. Passwords are interpolated into URLs, so note that they must be URL-safe.
- **Gotcha to document in the report:** Postgres init scripts run only on an empty data volume. The M00 `pgdata` volume already exists, so reviewers must run `docker compose down -v` once.

### 4.8 Test harness (`tests/conftest.py`, `tests/factories.py`)
- Server selection: if `TEST_DATABASE_ADMIN_URL` is set (a superuser URL, `postgresql+psycopg://…/postgres`), use it; otherwise start `PostgresContainer("postgres:16-alpine")` once per session.
- **The `chat_app` role:** if the role doesn't exist, create it with a random password. If it already exists, as on the compose server, **never change its password**, since that would break the running API. Read the password from `TEST_CHAT_APP_PASSWORD` instead, and fail with a clear message if it isn't set.
- For each xdist worker (`worker_id`, which is `"master"` without xdist): drop and create `test_<worker_id>`, then run Alembic `upgrade head` programmatically, with `sqlalchemy.url` set, `configure_logger=False`, and `%` in URLs escaped as `%%`. Drop the database at session end.
- Default isolation: connect as `chat_app`, `conn.begin()`, then `AsyncSession(bind=conn, join_transaction_mode="create_savepoint")`, and give the UoW a session factory that returns that session. A UoW "commit" inside a test then only releases a savepoint, and the outer transaction rolls back at teardown.
- `@pytest.mark.real_commits`: real commits, with the tables truncated by the owner connection at teardown (`TRUNCATE … RESTART IDENTITY CASCADE`). Register the marker in `pyproject.toml`; `--strict-markers` is already on.
- The role's `idle_in_transaction_session_timeout = 10s` also applies to the test session. Pausing in a debugger inside a test for more than 10 s kills its connection. Mention this in a comment on the fixture.
- `tests/factories.py`: `async def make_user(session, **overrides) -> User` with unique defaults.
- CI: testcontainers works on `ubuntu-latest` (Docker is available). Add the step
  `! grep -rnE "\.(commit|rollback)\(" app --include=*.py | grep -v "^app/platform/db.py"`.

## 5. Tests required (from M01, plus those implied by this brief)

- **Unit:**
  - UoW commit/rollback/close behavior with a fake session.
  - `session` before entering raises.
  - Double enter raises.
  - Settings reject a missing or non-asyncpg `DATABASE_URL`.
- **Integration:**
  - UoW commits on a clean exit; rolls back and re-raises on an exception; a savepoint rollback leaves the outer transaction usable.
  - `citext` uniqueness is case-insensitive (`Alice` vs `alice` → `IntegrityError` whose `constraint_name == "uq_users_username_active"`). A soft-deleted user frees the username.
  - Alembic `upgrade head` → `downgrade base` → `upgrade head`.
  - The app role can't run DDL (`CREATE TABLE` → `permission denied`) but can SELECT/INSERT/UPDATE/DELETE on `users`.
  - `idle_in_transaction_session_timeout` is `10s` for `chat_app`.
  - Constraint registry: `ck_users_status`, `pk_users`, `uq_users_username_active`, `uq_users_email_active`, `ix_users_username_trgm` and `ix_users_display_name_trgm` exist in the migrated schema.
  - The trigram index is used: `EXPLAIN` of `username::text ILIKE '%ali%'` with `enable_seqscan = off` shows `ix_users_username_trgm`.
  - Repository: `get_by_username_or_email` matches case-insensitively and excludes soft-deleted users.
- **API:** `/health/ready` gives 200 with the DB up (integration fixture), and 503 with the envelope when the check fails (existing fake-check test). `/health/live` gives 200 when the DB is unreachable.

## 6. Out of scope for M01

Auth endpoints, password hashing, JWT, refresh tokens, any other table (`refresh_tokens`, `ws_tickets`, …), a `get_uow` FastAPI dependency (it arrives with the first route that needs it, in M02), the outbox, the worker, and frontend changes.

## 7. Acceptance checklist to report (PASS/FAIL with evidence)

1. `docker compose down -v && docker compose up -d`: `migrate` exits 0, `api` becomes healthy.
2. `api` connects as `chat_app` (`SELECT current_user` through the app's engine, or `pg_stat_activity`).
3. `/health/ready` returns 503 with `docker compose stop db`, then 200 after `docker compose start db`, with no API restart.
4. `uv run pytest -n auto` passes locally, using testcontainers.
5. The tests pass in the container against the compose db:
   `docker compose run --rm -e TEST_DATABASE_ADMIN_URL=postgresql+psycopg://chat_owner:<pw>@db:5432/postgres -e TEST_CHAT_APP_PASSWORD=<pw> api pytest`. Add a Makefile target for this.
6. `ruff check`, `ruff format --check` and `mypy --strict app` are clean, and the CI commit-grep step passes.
7. The migration was reviewed by hand, and the round-trip test passes.
8. No `commit()`/`rollback()` outside `app/platform/db.py`. No SQLAlchemy imports in `domain/`.
9. `DATABASE_OWNER_URL` appears only in the `migrate` service's environment.
