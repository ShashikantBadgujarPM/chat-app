# M00 — Foundation

## Goal
Get a runnable skeleton in place: the backend and frontend projects, a Docker Compose stack, configuration, structured logging with request ids, the error hierarchy with its HTTP translation, health endpoints and CI. There are no business features yet. Everything later builds on these conventions, so they need to be right first.

## Dependencies
None.

## Files/modules expected
- `backend/pyproject.toml` (Python 3.12; deps: fastapi, uvicorn[standard], pydantic v2, pydantic-settings, sqlalchemy[asyncio] 2.x, asyncpg, alembic, psycopg[binary] (for Alembic), argon2-cffi, pyjwt; dev: pytest, pytest-asyncio, pytest-xdist, pytest-cov, httpx, testcontainers[postgres], ruff, mypy), and a lockfile (`uv.lock`)
- `backend/app/main.py`: `create_app()` factory, lifespan skeleton, router registration
- `backend/app/config.py`: `Settings` with the fail-fast validations from [12 §22.4](../design/12-local-dev-docker.md) and [10 §20](../design/10-errors-logging-security.md) (secure configuration)
- `backend/app/platform/logging.py`: `dictConfig`, JSON and console formatters, `LogContext` contextvar + filter, `RedactingFilter`
- `backend/app/platform/middleware.py`: request-id middleware, access-log middleware (route template, status, duration), body-size limit middleware
- `backend/app/platform/errors.py`: `AppError` hierarchy + exception handlers + error envelope
- `backend/app/platform/health.py`: `/health/live`, `/health/ready` (the DB check is stubbed until M01)
- `backend/Dockerfile` (multi-stage, non-root), `docker-compose.yml`, `docker-compose.override.yml`, `.env.example`, `.gitignore`, `.dockerignore`, `Makefile`
- `frontend/` Vite + React + TS scaffold, with the dev proxy for `/api`, `/ws` and `/health`
- `.github/workflows/ci.yml` (or an equivalent script): ruff, mypy, pytest, tsc
- `backend/tests/conftest.py`, `backend/tests/unit/platform/…`

## Database changes
None. The `db` service runs, but there's no schema yet.

## API changes
- `GET /health/live` → `200 {"status":"ok"}`
- `GET /health/ready` → `200` (the DB check is added in M01)
- A temporary `GET /api/v1/_debug/error` route, available only when `ENV=test`, that raises each `AppError` subtype so the handlers can be tested

## WebSocket changes
None.

## Tests
- Unit: `Settings` rejects a missing or short `JWT_SECRET`; `COOKIE_SECURE=false` outside `ENV=development`; and, in production, `DEBUG=true` and `*` CORS.
- Unit: `RedactingFilter` masks `password`/`token`/`authorization` keys and JWT-looking strings.
- Unit: the `LogContext` contextvar is isolated between two concurrent tasks (`asyncio.gather`).
- API: `X-Request-ID` is echoed when valid and replaced when invalid; every response has the header.
- API: each `AppError` subtype → the documented status and envelope; an unhandled exception → 500 with a generic message and `request_id`, and the stack trace shows up in the captured logs but not in the body.
- API: a request body over 64 KB → 413.

## Acceptance criteria
- `docker compose up` starts `db`, `api` and `web`; `curl localhost:8000/health/live` returns 200; the Vite page loads at `localhost:5173` and `/health/live` goes through the proxy.
- The API logs are one JSON object per line and include `request_id`, `route`, `status` and `duration_ms`.
- `ruff`, `mypy --strict app/` and `pytest` pass in CI.
- Nothing in the repo contains a real secret; `.env` is git-ignored.
