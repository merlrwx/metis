"""Republish stale pending jobs after a database/queue publication interruption."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from backend import database, observability
from backend.models import IngestionJob
from backend.queue import broker
from backend.tasks import process_ingestion_job


async def reconcile_pending_jobs(
    session, publisher, minimum_age_seconds=120, limit=100
):
    pending = list(
        session.scalars(
            select(IngestionJob)
            .where(
                IngestionJob.status == "pending",
                IngestionJob.created_at
                < datetime.now(UTC) - timedelta(seconds=minimum_age_seconds),
            )
            .order_by(IngestionJob.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    reconciled = 0
    # Keep row locks until publication and status updates commit together.
    try:
        for job in pending:
            await publisher(str(job.id), str(job.organisation_id))
            job.status = "queued"
            job.error = None
            reconciled += 1
            observability.log_event(
                "job_reconciled",
                job_id=str(job.id),
                organisation_id=str(job.organisation_id),
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    observability.RECONCILED.inc(reconciled)
    return reconciled


async def run():
    observability.configure_logging()
    await broker.startup()
    try:
        with database.SessionLocal() as session:
            await reconcile_pending_jobs(session, process_ingestion_job.kiq)
    finally:
        await broker.shutdown()


def main():
    asyncio.run(run())
