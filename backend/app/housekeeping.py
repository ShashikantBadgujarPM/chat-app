"""Housekeeping jobs the worker runs (docs/design/09 §17.1).

Each batch runs in its own transaction under `pg_try_advisory_xact_lock(key)`: with
N workers, only one works on a job at a time and the others skip it (R-17). The lock
is released at commit, so a crashed worker can never leave it held.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.db import SessionFactory, UnitOfWork

logger = logging.getLogger(__name__)

OUTBOX_DELETE_BATCH = 5000

# (session) -> rows deleted in one batch
JobBatch = Callable[[AsyncSession], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class HousekeepingJob:
    name: str
    advisory_key: int  # unique per job
    interval_seconds: float
    batch: JobBatch
    batch_size: int | None = None  # set: repeat while a batch comes back full


async def run_job(session_factory: SessionFactory, job: HousekeepingJob) -> int | None:
    """Run a job to completion. None if another worker holds its lock."""
    total = 0
    while True:
        async with UnitOfWork(session_factory) as uow:
            locked: bool = (
                await uow.session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": job.advisory_key}
                )
            ).scalar_one()
            if not locked:
                logger.debug(
                    "Housekeeping job skipped: running elsewhere",
                    extra={"event": "housekeeping.skipped", "job": job.name},
                )
                return None if total == 0 else total
            deleted = await job.batch(uow.session)
        total += deleted
        if job.batch_size is None or deleted < job.batch_size:
            break
    logger.info(
        "Housekeeping job finished",
        extra={"event": "housekeeping.finished", "job": job.name, "deleted": total},
    )
    return total


def outbox_retention(days: int, batch_size: int = OUTBOX_DELETE_BATCH) -> JobBatch:
    async def batch(session: AsyncSession) -> int:
        result = await session.execute(
            text(
                """
                DELETE FROM event_outbox WHERE id IN (
                    SELECT id FROM event_outbox
                    WHERE created_at < now() - make_interval(days => :days)
                    ORDER BY id
                    LIMIT :batch_size
                )
                """
            ),
            {"days": days, "batch_size": batch_size},
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    return batch


async def expired_ws_tickets(session: AsyncSession) -> int:
    result = await session.execute(
        text("DELETE FROM ws_tickets WHERE expires_at < now() - interval '1 day'")
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


async def expired_refresh_tokens(session: AsyncSession) -> int:
    result = await session.execute(
        text("DELETE FROM refresh_tokens WHERE expires_at < now() - interval '30 days'")
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def default_jobs(*, outbox_retention_days: int) -> list[HousekeepingJob]:
    return [
        HousekeepingJob(
            "outbox_retention",
            advisory_key=817_001,
            interval_seconds=3600,
            batch=outbox_retention(outbox_retention_days),
            batch_size=OUTBOX_DELETE_BATCH,
        ),
        HousekeepingJob(
            "ws_ticket_cleanup",
            advisory_key=817_002,
            interval_seconds=600,
            batch=expired_ws_tickets,
        ),
        HousekeepingJob(
            "refresh_token_cleanup",
            advisory_key=817_003,
            interval_seconds=3600,
            batch=expired_refresh_tokens,
        ),
    ]
