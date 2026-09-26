# Convenience wrappers (docs/design/12 §22.6). Every target is a plain command you can
# also run directly, which is handy on Windows without make.

# Makes .env values (e.g. the dev database passwords) available as make variables.
-include .env

.PHONY: env up down logs migrate revision test test-docker lint format typecheck check web-check

env:
	test -f .env || cp .env.example .env

up: env
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f api

migrate:
	docker compose run --rm migrate

# Usage: make revision m="add refresh tokens". Review the generated file by hand.
revision:
	docker compose run --rm migrate alembic revision --autogenerate -m "$(m)"

# Local run: integration tests start a Postgres testcontainer (Docker required).
test:
	cd backend && uv run pytest -n auto

# In the api container, against the compose db (docs/design/11 §21.3).
# The passwords come from .env (included above).
test-docker:
	docker compose run --rm \
	  -e TEST_DATABASE_ADMIN_URL=postgresql+psycopg://chat_owner:$(POSTGRES_OWNER_PASSWORD)@db:5432/postgres \
	  -e TEST_CHAT_APP_PASSWORD=$(CHAT_APP_PASSWORD) \
	  api pytest -n auto

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

format:
	cd backend && uv run ruff format . && uv run ruff check --fix .

typecheck:
	cd backend && uv run mypy --strict app

web-check:
	cd frontend && npm run typecheck

check: lint typecheck test web-check
