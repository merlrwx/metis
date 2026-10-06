import asyncio
import os
import uuid
from time import perf_counter

from sqlalchemy import delete, select
from taskiq import TaskiqEvents

from backend import database, embeddings, observability, worker_monitor
from backend.models import Chunk, DocumentVersion
from backend.queue import broker
from backend.services import chunking, ingestion, jobs
from backend.storage import get_object_storage

broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, worker_monitor.start)
broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, worker_monitor.stop)


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
        job = jobs.get_job(session, parsed_organisation_id, parsed_job_id)
        document_version_id = job.document_version_id if job is not None else None
    if attempt is None:
        return

    started = perf_counter()
    observability.JOBS_STARTED.inc()
    if attempt > 1:
        observability.RETRIES.inc()
    observability.log_event(
        "job_started", job_id=job_id, organisation_id=organisation_id, attempt=attempt
    )
    try:
        if document_version_id is None:
            await asyncio.sleep(
                float(os.environ.get("METIS_TEST_JOB_DELAY_SECONDS", "0.1"))
            )
            failure_count = int(os.environ.get("METIS_TEST_JOB_FAIL_ATTEMPTS", "0"))
            if attempt <= failure_count:
                raise RuntimeError("simulated transient worker failure")
        else:
            with database.SessionLocal() as session:
                version = session.scalar(
                    select(DocumentVersion).where(
                        DocumentVersion.organisation_id == parsed_organisation_id,
                        DocumentVersion.id == document_version_id,
                    )
                )
                if version is None:
                    raise RuntimeError("Ingestion document version was not found")
                object_key, filename, mime_type = (
                    version.object_key,
                    version.filename,
                    version.mime_type,
                )
            storage = get_object_storage()
            data = await asyncio.to_thread(storage.get, object_key)
            extracted = await asyncio.to_thread(
                ingestion.extract_document, filename, mime_type, data
            )
            text_chunks = chunking.split_document(extracted.text, extracted.metadata)
            if not text_chunks:
                raise RuntimeError("Document extraction produced no text chunks")
            provider = embeddings.get_embedding_provider()
            observability.EMBEDDINGS.inc()
            vectors = await asyncio.to_thread(
                provider.embed_documents, [chunk.content for chunk in text_chunks]
            )
            if len(vectors) != len(text_chunks):
                raise RuntimeError(
                    "Embedding provider returned the wrong number of vectors"
                )
            with database.SessionLocal() as session:
                version = session.scalar(
                    select(DocumentVersion).where(
                        DocumentVersion.organisation_id == parsed_organisation_id,
                        DocumentVersion.id == document_version_id,
                    )
                )
                if version is None:
                    raise RuntimeError("Ingestion document version was not found")
                version.extracted_text = extracted.text
                version.extraction_metadata = extracted.metadata
                session.execute(
                    delete(Chunk).where(
                        Chunk.organisation_id == parsed_organisation_id,
                        Chunk.document_version_id == document_version_id,
                    )
                )
                session.add_all(
                    Chunk(
                        organisation_id=parsed_organisation_id,
                        document_version_id=document_version_id,
                        chunk_index=index,
                        content=chunk.content,
                        start_offset=chunk.start_offset,
                        end_offset=chunk.end_offset,
                        page=chunk.page,
                        section=chunk.section,
                        chunk_metadata=chunk.metadata,
                        embedding_model=provider.model_id,
                        embedding=vector,
                    )
                    for index, (chunk, vector) in enumerate(zip(text_chunks, vectors))
                )
                jobs.mark_indexed(session, parsed_organisation_id, parsed_job_id)
            observability.DOCUMENTS.inc()
            observability.CHUNKS.inc(len(text_chunks))
    except Exception as error:
        observability.JOBS_FAILED.inc()
        observability.log_event(
            "job_attempt_failed",
            job_id=job_id,
            organisation_id=organisation_id,
            attempt=attempt,
            error_type=type(error).__name__,
        )
        with database.SessionLocal() as session:
            should_retry = jobs.record_failure(
                session, parsed_organisation_id, parsed_job_id, str(error)
            )
        if should_retry:
            raise
        return
    finally:
        observability.JOB_DURATION.observe(perf_counter() - started)

    if document_version_id is None:
        with database.SessionLocal() as session:
            jobs.mark_completed(session, parsed_organisation_id, parsed_job_id)

    observability.JOBS_COMPLETED.inc()
    observability.log_event(
        "job_completed", job_id=job_id, organisation_id=organisation_id, attempt=attempt
    )
