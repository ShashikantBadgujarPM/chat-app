# syntax=docker/dockerfile:1
# Multi-stage build (docs/design/12 §22.2). The `api` and, later, `worker` services use
# the same image with a different command.

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# --- builder: resolve the locked dependencies into /opt/venv -------------------------
FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

# --- dev: adds the dev dependency group and the tests (used by the compose override) --
FROM builder AS dev
RUN uv sync --frozen
RUN groupadd --system app && useradd --system --gid app --create-home app
COPY app ./app
COPY tests ./tests
# /app is read-only for the non-root user, so skip pytest's cache directory.
ENV PATH=/opt/venv/bin:$PATH \
    PYTEST_ADDOPTS="-p no:cacheprovider"
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- runtime: no build tools, no dev dependencies, non-root -------------------------
FROM base AS runtime
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    PATH=/opt/venv/bin:$PATH
RUN groupadd --system app && useradd --system --gid app --no-create-home app
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
USER app
EXPOSE 8000
# --workers 1 is deliberate: WebSocket state lives in the process (ADR-002).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
