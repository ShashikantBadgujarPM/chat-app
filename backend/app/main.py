"""FastAPI application factory and composition root.

`uvicorn app.main:app` serves the module-level `app`. Tests call `create_app()` with
their own `Settings`.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.platform import debug, health
from app.platform.errors import UnhandledExceptionMiddleware, register_exception_handlers
from app.platform.logging import configure_logging
from app.platform.middleware import (
    AccessLogMiddleware,
    BodySizeLimitMiddleware,
    RequestIdMiddleware,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings, service="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Resources that live for the whole process (DB engine in M01, outbox
        # listener in M06) are created here and released in the `finally` block.
        logger.info(
            "Application started",
            extra={"event": "app.startup", "config": settings.safe_summary()},
        )
        try:
            yield
        finally:
            logger.info("Application stopped", extra={"event": "app.shutdown"})

    is_production = settings.env == "production"
    app = FastAPI(
        title="Chat App API",
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
        # OpenAPI docs are disabled in production (docs/design/10 §20).
        docs_url=None if is_production else "/docs",
        redoc_url=None,
        openapi_url=None if is_production else "/openapi.json",
    )
    app.state.settings = settings
    health.init_readiness_checks(app)

    register_exception_handlers(app)

    # add_middleware wraps the current stack, so the last one added is outermost.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(UnhandledExceptionMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    if settings.env == "test":
        app.include_router(debug.router)

    return app


app = create_app()
