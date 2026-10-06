"""Resume a tenant-scoped maintenance rebuild through existing ingestion workers."""

import argparse
import asyncio
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

from sqlalchemy import func, select, text

from backend import database, embeddings
from backend.models import Chunk, Document, IngestionJob, Organisation, Source
from backend.queue import broker
from backend.tasks import process_ingestion_job


def active_jobs(session, organisation_id):
    return list(
        session.scalars(
            select(IngestionJob)
            .join(
                Document,
                Document.current_version_id == IngestionJob.document_version_id,
            )
            .where(
                Document.organisation_id == organisation_id,
                IngestionJob.organisation_id == organisation_id,
            )
        )
    )


def indexable_jobs(session, organisation_id):
    # Never-indexed failed documents keep their visible failure and are not a model migration.
    return [
        job
        for job in active_jobs(session, organisation_id)
        if job.status == "indexed"
        or session.scalar(
            select(Chunk.id)
            .where(
                Chunk.organisation_id == organisation_id,
                Chunk.document_version_id == job.document_version_id,
            )
            .limit(1)
        )
        is not None
    ]


async def rebuild(organisation_id, timeout=300):
    if database.engine.dialect.name != "postgresql":
        raise ValueError("Re-indexing requires PostgreSQL")
    with database.engine.connect() as guard:
        key = str(organisation_id)
        locked = guard.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key}
        )
        if not locked:
            raise RuntimeError(
                "Another re-index operation is already running for this organisation"
            )
        try:
            await _rebuild(organisation_id, timeout)
        finally:
            guard.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                {"key": key},
            )


async def _rebuild(organisation_id, timeout=300):
    provider = embeddings.get_embedding_provider()
    provider.embed_query("Synthetic re-index readiness check")
    with database.SessionLocal() as session:
        organisation = session.scalar(
            select(Organisation)
            .where(Organisation.id == organisation_id)
            .with_for_update()
        )
        if organisation is None:
            raise ValueError("Organisation not found")
        if organisation.index_status == "ready" and (
            any(
                job.status in {"pending", "queued", "processing"}
                for job in active_jobs(session, organisation_id)
            )
            or session.scalar(
                select(Source.id).where(
                    Source.organisation_id == organisation_id,
                    Source.sync_started_at.is_not(None),
                )
            )
        ):
            raise ValueError(
                "Wait for existing ingestion and source synchronization before re-indexing"
            )
        organisation.index_status = "reindexing"
        session.commit()
    await broker.startup()
    try:
        with database.SessionLocal() as session:
            job_ids = [job.id for job in indexable_jobs(session, organisation_id)]
            skipped = len(active_jobs(session, organisation_id)) - len(job_ids)
        for job_id in job_ids:
            with database.SessionLocal() as session:
                job = session.get(IngestionJob, job_id)
                matching = session.scalar(
                    select(func.count())
                    .select_from(Chunk)
                    .where(
                        Chunk.organisation_id == organisation_id,
                        Chunk.document_version_id == job.document_version_id,
                        Chunk.embedding_model == provider.model_id,
                    )
                )
                if matching and job.status == "indexed":
                    continue
                if job.status != "processing":
                    job.status, job.attempts, job.error = "pending", 0, None
                    job.started_at, job.completed_at = None, None
                    session.commit()
                    await process_ingestion_job.kiq(str(job.id), str(organisation_id))
            deadline = monotonic() + timeout
            while monotonic() < deadline:
                with database.SessionLocal() as session:
                    job = session.get(IngestionJob, job_id)
                    if job.status == "failed":
                        raise RuntimeError(
                            "Ingestion failed; repair the dependency and repeat re-indexing"
                        )
                    if job.status == "indexed":
                        break
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError(
                    "Re-index timed out; maintenance remains active, repeat to resume"
                )
        with database.SessionLocal() as session:
            for job in indexable_jobs(session, organisation_id):
                models = set(
                    session.scalars(
                        select(Chunk.embedding_model).where(
                            Chunk.organisation_id == organisation_id,
                            Chunk.document_version_id == job.document_version_id,
                        )
                    )
                )
                if models != {provider.model_id} or job.status != "indexed":
                    raise RuntimeError(
                        "Worker model mismatch or missing chunks; maintenance remains active"
                    )
            organisation = session.get(Organisation, organisation_id)
            organisation.index_model = provider.model_id
            organisation.index_status = "ready"
            session.commit()
        print(
            f"Re-index completed for {organisation_id}; model={provider.model_id}, jobs={len(job_ids)}, failed_without_index_skipped={skipped}, completed_at={datetime.now(UTC).isoformat()}"
        )
    finally:
        await broker.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("organisation_id", type=UUID)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    asyncio.run(rebuild(args.organisation_id, args.timeout))
