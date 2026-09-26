# Convenience wrappers (docs/design/12 §22.6). Every target is a plain command you can
# also run directly, which is handy on Windows without make.

.PHONY: env up down logs test lint format typecheck check web-check

env:
	test -f .env || cp .env.example .env

up: env
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f api

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

format:
	cd backend && uv run ruff format . && uv run ruff check --fix .

typecheck:
	cd backend && uv run mypy --strict app

web-check:
	cd frontend && npm run typecheck

check: lint typecheck test web-check
