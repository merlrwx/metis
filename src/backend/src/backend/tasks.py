import asyncio
import os
import uuid

from backend import database
from backend.queue import broker
from backend.services import jobs


@broker.task
async def wake_worker() -> None:
    """Wake a restarted stream consumer so it can reclaim pending work."""


@broker.task(
    retry_on_error=True,
    max_retries=jobs.MAX_JOB_ATTEMPTS,
    delay=0.1,
)
async def process_ingestion_job(job_id: str, organisation_id: str) -> None:
    await run_ingestion_job(job_id, organisation_id)


async def run_ingestion_job(job_id: str, organisation_id: str) -> None:
    parsed_job_id = uuid.UUID(job_id)
    parsed_organisation_id = uuid.UUID(organisation_id)
    with database.SessionLocal() as session:
        attempt = jobs.start_attempt(session, parsed_organisation_id, parsed_job_id)
    if attempt is None:
        return

    try:
        await asyncio.sleep(
            float(os.environ.get("METIS_TEST_JOB_DELAY_SECONDS", "0.1"))
        )
        failure_count = int(os.environ.get("METIS_TEST_JOB_FAIL_ATTEMPTS", "0"))
        if attempt <= failure_count:
            raise RuntimeError("simulated transient worker failure")
    except Exception as error:
        with database.SessionLocal() as session:
            should_retry = jobs.record_failure(
                session, parsed_organisation_id, parsed_job_id, str(error)
            )
        if should_retry:
            raise
        return

    with database.SessionLocal() as session:
        jobs.mark_completed(session, parsed_organisation_id, parsed_job_id)
