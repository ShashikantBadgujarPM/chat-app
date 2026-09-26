"""Pure ASGI middlewares for request ids, access logging and the body-size limit.

Pure ASGI (rather than Starlette's BaseHTTPMiddleware) keeps the ContextVar set in
the same task that runs the endpoint and avoids buffering streaming responses.

Order, outermost first (see `app.main.create_app`):
    RequestIdMiddleware -> AccessLogMiddleware -> UnhandledExceptionMiddleware
    -> BodySizeLimitMiddleware -> FastAPI routing
"""

import logging
import re
import time
from uuid import uuid4

from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.platform.errors import error_response
from app.platform.logging import bind_log_context, get_log_context

REQUEST_ID_HEADER = "X-Request-ID"
# Anything else is replaced, so a client can't inject content into log lines.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

access_logger = logging.getLogger("app.access")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and _VALID_REQUEST_ID.fullmatch(incoming) else uuid4().hex

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with bind_log_context(request_id=request_id):
            await self.app(scope, receive, send_with_header)


class AccessLogMiddleware:
    """Writes one `http.request` line per request, with the route template."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 500  # if the app raises before responding
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # FastAPI's router stores the matched route on the (shared) scope dict.
            route = getattr(scope.get("route"), "path", None)
            # Set by get_current_user on request.state (scope["state"]); the log context
            # itself has already been reset when this runs.
            user_id = scope.get("state", {}).get("user_id") or get_log_context().user_id
            access_logger.log(
                logging.ERROR if status_code >= 500 else logging.INFO,
                "%s %s %s",
                scope["method"],
                route or "<unmatched>",
                status_code,
                extra={
                    "event": "http.request",
                    "method": scope["method"],
                    # The template (/conversations/{id}), never the raw path.
                    "route": route,
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "user_id": user_id,
                },
            )


class BodySizeLimitMiddleware:
    """Rejects request bodies larger than `max_bytes` with 413 (docs/design/10 §20)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._too_large()(scope, receive, send)
            return

        # A chunked body has no Content-Length, so count what is actually read.
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI re-raises HTTPException from body reading unchanged,
                    # so this reaches the envelope handler as a 413.
                    raise HTTPException(status_code=413)
            return message

        await self.app(scope, limited_receive, send)

    def _too_large(self) -> ASGIApp:
        return error_response(413, "payload_too_large", "The request body is too large.")
