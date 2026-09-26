"""Supervised background tasks (docs/design/13 §24.1).

A task that dies silently is a classic asyncio bug: `asyncio.create_task` keeps only a
weak reference, and an unobserved exception is logged at best at garbage collection.
`TaskSupervisor` keeps strong references, logs every unexpected exception, and restarts
critical tasks with backoff.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

CoroutineFactory = Callable[[], Awaitable[None]]


class TaskSupervisor:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def spawn(self, coro: Awaitable[None], *, name: str) -> asyncio.Task[None]:
        """Run once; log it if it fails."""

        async def run() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Background task failed", extra={"event": "task.failed", "task": name}
                )

        return self._track(asyncio.create_task(run(), name=name))

    def spawn_supervised(
        self,
        factory: CoroutineFactory,
        *,
        name: str,
        initial_backoff: float = 0.5,
        max_backoff: float = 30.0,
    ) -> asyncio.Task[None]:
        """Run `factory()` forever: restart it with backoff whenever it returns or fails."""

        async def run() -> None:
            backoff = initial_backoff
            while not self._stopping:
                try:
                    await factory()
                    backoff = initial_backoff  # a clean return is not a failure
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Supervised task crashed; restarting",
                        extra={"event": "task.restarted", "task": name, "backoff_s": backoff},
                    )
                if self._stopping:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        return self._track(asyncio.create_task(run(), name=name))

    def _track(self, task: asyncio.Task[None]) -> asyncio.Task[None]:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self, grace_seconds: float = 5.0) -> None:
        self._stopping = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=grace_seconds)
