"""FastAPI application factory and composition root.

`uvicorn app.main:app` serves the module-level `app`. Tests call `create_app()` with
their own `Settings`.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.modules.conversations.api import router as conversations_router
from app.modules.identity.api import routers as identity_routers
from app.modules.identity.api import users_router
from app.modules.identity.api.deps import register_identity_exception_handlers
from app.modules.messaging.api import routers as messaging_routers
from app.platform import debug, health
from app.platform.clock import SystemClock
from app.platform.db import check_database, create_engine, create_session_factory
from app.platform.errors import UnhandledExceptionMiddleware, register_exception_handlers
from app.platform.logging import configure_logging
from app.platform.middleware import (
    AccessLogMiddleware,
    BodySizeLimitMiddleware,
    RequestIdMiddleware,
)
from app.platform.rate_limit import TokenBucketLimiter
from app.platform.security import Argon2PasswordHasher, JwtTokenIssuer
from app.platform.tasks import TaskSupervisor
from app.realtime import gateway
from app.realtime.connection_manager import ConnectionManager
from app.realtime.outbox_listener import OutboxListener, libpq_dsn

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings, service="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Process-wide resources are created here and released in `finally`.
        # Creating the engine doesn't connect, so startup succeeds with the DB down:
        # /health/live stays 200 and /health/ready reports 503 until it's reachable.
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        health.register_readiness_check(app, "database", partial(check_database, engine))

        # Realtime (docs/design/08 §14.3-14.4): connections, and the listener that
        # delivers committed outbox events to them. Supervised: restarted if it dies.
        tasks = TaskSupervisor()
        app.state.tasks = tasks
        connections = ConnectionManager(supervisor=tasks, queue_size=settings.ws_send_queue_size)
        app.state.connections = connections
        listener = OutboxListener(
            dsn=libpq_dsn(settings.database_url.get_secret_value()),
            engine=engine,
            manager=connections,
        )
        app.state.outbox_listener = listener
        tasks.spawn_supervised(listener.run, name="outbox-listener")
        health.register_readiness_check(app, "outbox_listener", listener.check_ready)
        logger.info(
            "Application started",
            extra={"event": "app.startup", "config": settings.safe_summary()},
        )
        try:
            yield
        finally:
            # Graceful shutdown (08 §14.7): tell clients to reconnect, let queued frames
            # drain, then stop background tasks and close the pool.
            await connections.close_all(
                1001, "server_shutdown", drain_timeout=settings.ws_shutdown_drain_seconds
            )
            await tasks.shutdown()
            await engine.dispose()
            logger.info("Database engine disposed", extra={"event": "db.engine_disposed"})
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
    # Ports and process-wide services. Tests replace these (e.g. a FrozenClock).
    app.state.clock = SystemClock()
    app.state.password_hasher = Argon2PasswordHasher.from_settings(settings)
    app.state.token_issuer = JwtTokenIssuer.from_settings(settings)
    app.state.rate_limiter = TokenBucketLimiter()
    health.init_readiness_checks(app)

    register_exception_handlers(app)
    register_identity_exception_handlers(app)

    # add_middleware wraps the current stack, so the last one added is outermost.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(UnhandledExceptionMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(gateway.router)
    app.include_router(identity_routers.router)
    app.include_router(identity_routers.authenticated)
    app.include_router(users_router.router)
    app.include_router(conversations_router.router)
    app.include_router(messaging_routers.conversation_messages_router)
    app.include_router(messaging_routers.messages_router)
    if settings.env == "test":
        app.include_router(debug.router)

    return app


app = create_app()
