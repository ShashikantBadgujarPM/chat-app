"""Liveness and readiness endpoints (docs/design/07 §13.3 Health).

- `/health/live` says only that the process is up. It never touches a dependency,
  so an unavailable database cannot make the orchestrator restart a healthy process.
- `/health/ready` runs every registered readiness check. The registry is empty in
  M00; M01 registers the database check, and M06 the outbox listener check.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.platform.errors import error_response

logger = logging.getLogger(__name__)

# A check returns normally when its dependency is usable and raises otherwise.
ReadinessCheck = Callable[[], Awaitable[None]]

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    checks: dict[str, Literal["ok"]]


def init_readiness_checks(app: FastAPI) -> None:
    app.state.readiness_checks = {}


def register_readiness_check(app: FastAPI, name: str, check: ReadinessCheck) -> None:
    checks: dict[str, ReadinessCheck] = app.state.readiness_checks
    checks[name] = check


@router.get("/live")
async def live() -> LiveResponse:
    return LiveResponse()


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"description": "One or more dependencies are unavailable"}},
)
async def ready(request: Request) -> ReadyResponse | JSONResponse:
    checks: dict[str, ReadinessCheck] = request.app.state.readiness_checks
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            await check()
        except Exception as exc:
            # The reason goes to the log only; the response doesn't reveal internals.
            logger.warning(
                "Readiness check failed",
                extra={
                    "event": "health.check_failed",
                    "check": name,
                    "error_type": type(exc).__name__,
                },
            )
            results[name] = "unavailable"
        else:
            results[name] = "ok"

    if all(result == "ok" for result in results.values()):
        return ReadyResponse(checks=dict.fromkeys(results, "ok"))
    return error_response(
        503,
        "not_ready",
        "One or more dependencies are unavailable.",
        details={"checks": results},
    )
