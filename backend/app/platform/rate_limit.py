"""In-process token-bucket rate limiting (docs/design/07 §13.4, 10 §20).

Buckets live in the API process, which is correct only while there is one API
instance (ADR-002). Moving to a shared store is the documented upgrade path.

Client IP: uvicorn runs with --proxy-headers and --forwarded-allow-ips=TRUSTED_PROXIES,
so `request.client.host` already reflects X-Forwarded-For only when the request came
through a trusted proxy. The app never parses that header itself, so it can't be
spoofed from outside.
"""

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request

from app.platform.errors import RateLimitedError

KeyFunction = Callable[[Request], str | None]


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """Buckets keyed by (scope, key). `capacity` tokens, refilled at `rate` per second."""

    MAX_BUCKETS = 100_000  # memory bound; the oldest buckets are dropped first

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._clock = clock

    def try_acquire(self, scope: str, key: str, *, capacity: int, per_seconds: float) -> float:
        """Take one token. Returns 0 on success, else the seconds until one is available."""
        now = self._clock()
        rate = capacity / per_seconds
        bucket = self._buckets.get((scope, key))
        if bucket is None:
            if len(self._buckets) >= self.MAX_BUCKETS:
                self._evict_oldest()
            bucket = _Bucket(tokens=capacity, updated_at=now)
            self._buckets[(scope, key)] = bucket
        else:
            bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated_at) * rate)
            bucket.updated_at = now

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return 0.0
        return (1 - bucket.tokens) / rate

    def _evict_oldest(self) -> None:
        oldest = sorted(self._buckets, key=lambda k: self._buckets[k].updated_at)
        for key in oldest[: len(oldest) // 10 or 1]:
            del self._buckets[key]


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def rate_limited(
    scope: str,
    key_fn: KeyFunction,
    *,
    limit: Callable[[Request], tuple[int, float]],
) -> Callable[[Request], Awaitable[None]]:
    """A FastAPI dependency that enforces `limit(request) -> (capacity, per_seconds)`.

    The limit is read from the request so it can come from the app's Settings.
    """

    async def dependency(request: Request) -> None:
        if not request.app.state.settings.rate_limit_enabled:
            return
        key = key_fn(request)
        if key is None:
            return
        capacity, per_seconds = limit(request)
        limiter: TokenBucketLimiter = request.app.state.rate_limiter
        wait = limiter.try_acquire(scope, key, capacity=capacity, per_seconds=per_seconds)
        if wait > 0:
            raise RateLimitedError(retry_after_seconds=max(1, math.ceil(wait)))

    return dependency
