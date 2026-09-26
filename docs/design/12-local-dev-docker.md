# 22. Docker / Local Development Architecture

## 22.1 Compose services

| Service | Image | Command | Ports (dev) | Depends on | Healthcheck |
|---|---|---|---|---|---|
| `db` | `postgres:16-alpine` | default | `5432:5432` | — | `pg_isready -U chat_owner` |
| `migrate` | backend image | `alembic upgrade head` (one-shot, runs as the owner role) | — | `db` healthy | exits 0 |
| `api` | backend image | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --ws-ping-interval 20 --ws-ping-timeout 20 --proxy-headers --forwarded-allow-ips=<proxy>` | `8000:8000` | `migrate` completed successfully | `GET /health/ready` |
| `worker` | backend image | `python -m app.worker` | — | `migrate` completed successfully | heartbeat file age < 30 s |
| `web` | dev: `node:20` running `vite`; prod: nginx serving `dist/` | dev: `npm run dev -- --host` | `5173:5173` | `api` | — |

- **`--workers 1` is deliberate.** It follows from [ADR-002](../README.md#adr-002-single-api-instance-first): WebSocket connection state lives in the process. The config and README say so explicitly, so nobody "optimizes" it to 4 workers and breaks presence and fan-out.
- **Migrations run as a separate one-shot service**, not on API startup. Running them at startup races when several replicas start together, and it mixes privileges (migrations need DDL rights; the app role shouldn't have them).

## 22.2 Images

Backend Dockerfile, multi-stage:
1. `python:3.12-slim` builder: install `uv`, then `uv sync --frozen --no-dev` into `/opt/venv`.
2. Runtime: `python:3.12-slim`, copy the venv and `app/`, `alembic/`, `alembic.ini`; run as non-root `USER app`; `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1` (so logs aren't buffered). No compilers in the final image; the `argon2-cffi` and `asyncpg` wheels are prebuilt.

Dev override (`docker-compose.override.yml`, loaded automatically): bind-mounts `backend/app` and adds `--reload` to the API. The worker is restarted manually, because a reloader on a worker with open transactions confuses more than it helps.

## 22.3 Networking in development

The Vite dev server proxies `/api` → `http://api:8000`, `/ws` → `ws://api:8000` (with `ws: true`) and `/health` → `http://api:8000`, all with paths unchanged (health stays outside `/api/v1`, per [07 §13.3](07-rest-api.md)). The browser therefore talks to a **single origin** (`localhost:5173`), which gives:
- no CORS configuration needed in dev, and the dev setup matches production, where nginx does the same proxying;
- a first-party refresh cookie with `SameSite=Strict` that just works;
- `COOKIE_SECURE=false` allowed **only** when `ENV=development`, since dev runs over HTTP (the settings validation enforces this).

## 22.4 Configuration

`pydantic-settings` reads the environment. `.env.example` is committed; `.env` is git-ignored.

| Variable | Example / default | Notes |
|---|---|---|
| `ENV` | `development` \| `test` \| `production` | Drives the fail-fast validations |
| `DATABASE_URL` | `postgresql+asyncpg://chat_app:***@db:5432/chat` | App role (DML only) |
| `DATABASE_OWNER_URL` | `postgresql+psycopg://chat_owner:***@db:5432/chat` | Used only by Alembic, so it is given only to the `migrate` service, never to `api` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `5` | |
| `JWT_SECRET` | ≥ 32 bytes, random | Required. `JWT_SECRET_FILE` is also supported |
| `JWT_SECRET_PREVIOUS` | empty | Rotation window |
| `ACCESS_TOKEN_TTL_SECONDS` | `900` | |
| `REFRESH_TOKEN_TTL_DAYS` | `14` | |
| `COOKIE_SECURE` | `true` | `false` only allowed in development |
| `ALLOWED_ORIGINS` | `http://localhost:5173` | CORS + WS Origin check |
| `TRUSTED_PROXIES` | `172.16.0.0/12` | For `X-Forwarded-For` |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `console` in dev |
| `SCHEDULER_POLL_INTERVAL_SECONDS` | `2` | |
| `SCHEDULER_BATCH_SIZE` | `20` | |
| `SCHEDULER_MAX_ATTEMPTS` | `5` | |
| `SCHEDULE_MAX_LATENESS_HOURS` | `24` | |
| `OUTBOX_RETENTION_DAYS` | `7` | |
| `PRESENCE_GRACE_SECONDS` | `10` | |
| `APP_VERSION` | git sha (build arg) | Put in logs |

## 22.5 Database roles and initialization

Role creation and privileges are split by scope, because Postgres init scripts run only once, for one database, while every database the tests create must end up with identical privileges.

**Cluster level: an init script** (`db/init/01-roles.sh`, mounted into `docker-entrypoint-initdb.d`). It is a shell script rather than `.sql` because it reads the password from the environment (`CHAT_APP_PASSWORD`), which a plain `.sql` init file can't do. It creates:
- `chat_owner`: the image's `POSTGRES_USER`. It owns the schema and is used only by migrations;
- `chat_app`: `LOGIN`, with no other attributes. Nothing else is granted here.

**Database level: the first Alembic migration** (`0001`). It runs as `chat_owner` in every migrated database, including each test database, and:
- enables `pgcrypto`, `citext` and `pg_trgm` (this needs owner or superuser rights, another reason migrations run as `chat_owner`);
- grants `chat_app` `CONNECT` on the current database, `USAGE` on schema `public`, `SELECT/INSERT/UPDATE/DELETE` on tables and `USAGE` on sequences, and sets `ALTER DEFAULT PRIVILEGES` for the role running the migrations (the owner) so tables created by later migrations are covered too. There is **no** DDL for `chat_app`, and the `audit_logs` migration later revokes `UPDATE/DELETE` on that table;
- sets `ALTER ROLE chat_app SET idle_in_transaction_session_timeout = '10s'` (needed by the outbox replay reasoning in [08](08-websocket.md) §14.5). This is a role-level setting, so it is idempotent; it needs superuser or `CREATEROLE`, which `chat_owner` has locally and in the test containers. A managed production database may need it applied by an administrator instead.

The migration requires the `chat_app` role to exist already. The init script creates it for the compose database, and the test harness creates it for its server ([11 §21.3](11-testing.md)).

## 22.6 Developer workflow

```
cp .env.example .env
docker compose up -d db && docker compose run --rm migrate
docker compose up api worker web            # http://localhost:5173
docker compose run --rm api pytest          # or run pytest locally against the compose db
docker compose run --rm migrate alembic revision --autogenerate -m "..."   # then review the file by hand
```

A `Makefile` (or `justfile`) wraps these commands. On Windows, `docker compose` commands work the same way; the Makefile targets are there for convenience, not a requirement.

Autogenerated migrations **must be reviewed by hand**. Autogenerate doesn't handle the partial indexes' `WHERE` clauses reliably, and it can't produce generated columns (`search_vector`), triggers (the outbox NOTIFY), the circular FKs or extensions. Those are written by hand in the migration.
