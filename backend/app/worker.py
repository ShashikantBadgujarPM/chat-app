"""The worker process: `python -m app.worker` (docs/design/09 §17, M11).

Same image and codebase as the API. It runs:
- the scheduled-message loop (claim due rows with SKIP LOCKED, deliver, repeat),
- housekeeping loops, each under an advisory lock so N workers never double up,
- a heartbeat file for the container healthcheck, and a stats line every 60 s.

SIGTERM/SIGINT set an asyncio.Event: the in-flight batch finishes and commits, no new
batch starts, housekeeping stops, and the process exits 0 (09 §17.6).
"""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from app.config import Settings, get_settings
from app.housekeeping import HousekeepingJob, default_jobs, run_job
from app.modules.messaging.application.messaging_service import MessagingService
from app.modules.messaging.infrastructure.unit_of_work import SqlMessagingUnitOfWork
from app.modules.scheduling.application.delivery_service import (
    DeliveryPolicy,
    DeliveryService,
    OnClaimed,
)
from app.modules.scheduling.infrastructure.repository import ScheduledMessageRepository
from app.modules.scheduling.infrastructure.unit_of_work import SqlSchedulingUnitOfWork
from app.platform.clock import Clock, SystemClock
from app.platform.db import SessionFactory, UnitOfWork, create_engine, create_session_factory
from app.platform.logging import bind_log_context, configure_logging

logger = logging.getLogger(__name__)

ERROR_BACKOFF_SECONDS = 5.0


class Worker:
    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory,
        *,
        clock: Clock | None = None,
        on_claimed: OnClaimed | None = None,
        housekeeping: list[HousekeepingJob] | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._on_claimed = on_claimed
        self._jobs = (
            housekeeping
            if housekeeping is not None
            else default_jobs(outbox_retention_days=settings.outbox_retention_days)
        )
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.stopping = asyncio.Event()
        messaging = MessagingService(
            lambda: SqlMessagingUnitOfWork(session_factory), clock or SystemClock()
        )
        self._delivery = DeliveryService(
            lambda: SqlSchedulingUnitOfWork(session_factory),
            messaging,
            DeliveryPolicy(
                max_lateness=timedelta(seconds=settings.schedule_max_lateness_seconds),
                max_attempts=settings.schedule_max_attempts,
            ),
        )

    def request_stop(self) -> None:
        if not self.stopping.is_set():
            logger.info("Worker stopping", extra={"event": "scheduler.stopping"})
            self.stopping.set()

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake up at once when a stop is requested."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)

    async def run(self) -> None:
        with bind_log_context(worker_id=self.worker_id):
            logger.info(
                "Worker started",
                extra={
                    "event": "scheduler.started",
                    "poll_interval": self._settings.scheduler_poll_interval_seconds,
                    "batch_size": self._settings.scheduler_batch_size,
                },
            )
            background = [
                asyncio.create_task(self.housekeeping_loop(job), name=f"housekeeping:{job.name}")
                for job in self._jobs
            ]
            background.append(asyncio.create_task(self.heartbeat_loop(), name="heartbeat"))
            background.append(asyncio.create_task(self.stats_loop(), name="stats"))
            try:
                # Returns only after a stop request, with the last batch committed.
                await self.scheduled_loop()
            finally:
                for task in background:
                    task.cancel()
                await asyncio.gather(*background, return_exceptions=True)
                logger.info("Worker stopped", extra={"event": "scheduler.stopped"})

    async def run_batch(self) -> int:
        """One claim-and-deliver batch; returns how many rows it claimed."""
        with bind_log_context(batch_id=uuid4().hex[:16]):
            results = await self._delivery.deliver_due(
                self._settings.scheduler_batch_size, on_claimed=self._on_claimed
            )
        return len(results)

    async def scheduled_loop(self) -> None:
        batch_size = self._settings.scheduler_batch_size
        while not self.stopping.is_set():
            try:
                claimed = await self.run_batch()
            except Exception:
                # E.g. the database is down. The batch rolled back; its rows are still
                # pending and will be claimed again.
                logger.exception(
                    "Scheduler batch failed", extra={"event": "scheduler.batch_failed"}
                )
                await self._sleep(ERROR_BACKOFF_SECONDS)
                continue
            if claimed < batch_size:
                await self._sleep(self._settings.scheduler_poll_interval_seconds)
            # A full batch means a backlog: go again immediately.

    async def housekeeping_loop(self, job: HousekeepingJob) -> None:
        while not self.stopping.is_set():
            try:
                await run_job(self._session_factory, job)
            except Exception:
                logger.exception(
                    "Housekeeping job failed",
                    extra={"event": "housekeeping.failed", "job": job.name},
                )
            await self._sleep(job.interval_seconds)

    async def heartbeat_loop(self) -> None:
        path: Path = self._settings.worker_heartbeat_file
        while not self.stopping.is_set():
            try:
                await asyncio.to_thread(path.touch)
            except OSError:
                logger.warning(
                    "Heartbeat file not writable", extra={"event": "worker.heartbeat_failed"}
                )
            await self._sleep(self._settings.worker_heartbeat_interval_seconds)

    async def stats_loop(self) -> None:
        while not self.stopping.is_set():
            await self._sleep(self._settings.scheduler_stats_interval_seconds)
            if self.stopping.is_set():
                return
            try:
                async with UnitOfWork(self._session_factory) as uow:
                    stats = await ScheduledMessageRepository(uow.session).stats()
                logger.info(
                    "Scheduler stats",
                    extra={
                        "event": "scheduler.stats",
                        "pending_total": stats.pending_total,
                        "due_now": stats.due_now,
                        "oldest_due_lag_s": round(stats.oldest_due_lag_s, 1),
                    },
                )
            except Exception:
                logger.exception(
                    "Scheduler stats failed", extra={"event": "scheduler.stats_failed"}
                )


def install_signal_handlers(worker: Worker) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows: no loop signal handlers; hop onto the loop from the handler.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(worker.request_stop))


async def main() -> None:
    settings = get_settings()
    configure_logging(settings, service="worker")
    engine = create_engine(settings)
    try:
        worker = Worker(settings, create_session_factory(engine))
        install_signal_handlers(worker)
        await worker.run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
