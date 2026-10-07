import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import Document, DocumentVersion, IngestionJob, Organisation

MAX_JOB_ATTEMPTS = 3


def create_test_job(
    session: Session, organisation_id: uuid.UUID, idempotency_key: str | None
) -> tuple[IngestionJob | None, bool]:
    if session.get(Organisation, organisation_id) is None:
        return None, False
    if idempotency_key:
        existing = session.scalar(
            select(IngestionJob).where(
                IngestionJob.organisation_id == organisation_id,
                IngestionJob.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing, False

    job = IngestionJob(
        organisation_id=organisation_id,
        idempotency_key=idempotency_key,
        status="pending",
    )
    session.add(job)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if idempotency_key:
            existing = session.scalar(
                select(IngestionJob).where(
                    IngestionJob.organisation_id == organisation_id,
                    IngestionJob.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing, False
        raise
    session.refresh(job)
    return job, True


def get_job(
    session: Session, organisation_id: uuid.UUID, job_id: uuid.UUID
) -> IngestionJob | None:
    return session.scalar(
        select(IngestionJob).where(
            IngestionJob.organisation_id == organisation_id,
            IngestionJob.id == job_id,
        )
    )


def retry_failed_document_job(
    session: Session,
    organisation_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    allow_indexed: bool = False,
) -> IngestionJob | None:
    job = session.scalar(
        select(IngestionJob)
        .join(
            DocumentVersion,
            (DocumentVersion.id == IngestionJob.document_version_id)
            & (DocumentVersion.organisation_id == IngestionJob.organisation_id),
        )
        .join(
            Document,
            (Document.id == DocumentVersion.document_id)
            & (Document.organisation_id == DocumentVersion.organisation_id),
        )
        .where(
            IngestionJob.organisation_id == organisation_id,
            Document.organisation_id == organisation_id,
            Document.id == document_id,
            Document.current_version_id == DocumentVersion.id,
            Document.deleted_at.is_(None),
            IngestionJob.status.in_(
                ["failed", "indexed", "completed"] if allow_indexed else ["failed"]
            ),
        )
        .with_for_update()
    )
    if job is None:
        return None
    job.status = "pending"
    job.attempts = 0
    job.error = None
    job.started_at = None
    job.completed_at = None
    session.commit()
    session.refresh(job)
    return job


def mark_queued(
    session: Session, organisation_id: uuid.UUID, job_id: uuid.UUID
) -> None:
    session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.organisation_id == organisation_id,
            IngestionJob.id == job_id,
            IngestionJob.status == "pending",
        )
        .values(status="queued", error=None)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    session.expire_all()


def record_enqueue_failure(
    session: Session, organisation_id: uuid.UUID, job_id: uuid.UUID, error: str
) -> None:
    job = get_job(session, organisation_id, job_id)
    if job is not None and job.status == "pending":
        job.error = error[:4096]
        session.commit()


def start_attempt(
    session: Session, organisation_id: uuid.UUID, job_id: uuid.UUID
) -> int | None:
    job = session.scalar(
        select(IngestionJob)
        .where(
            IngestionJob.organisation_id == organisation_id,
            IngestionJob.id == job_id,
        )
        .with_for_update()
    )
    if job is None or job.status in {"completed", "indexed", "failed"}:
        return None
    job.status = "processing"
    job.attempts += 1
    job.started_at = datetime.now(UTC)
    job.error = None
    session.commit()
    return job.attempts


def record_failure(
    session: Session,
    organisation_id: uuid.UUID,
    job_id: uuid.UUID,
    error: str,
) -> bool:
    job = get_job(session, organisation_id, job_id)
    if job is None or job.status in {"completed", "indexed", "failed"}:
        return False
    should_retry = job.attempts < MAX_JOB_ATTEMPTS
    job.status = "queued" if should_retry else "failed"
    job.error = error[:4096]
    if not should_retry:
        job.completed_at = datetime.now(UTC)
    session.commit()
    return should_retry


def mark_completed(
    session: Session, organisation_id: uuid.UUID, job_id: uuid.UUID
) -> None:
    job = get_job(session, organisation_id, job_id)
    if job is not None and job.status == "processing":
        job.status = "completed"
        job.error = None
        job.completed_at = datetime.now(UTC)
        session.commit()


def mark_indexed(
    session: Session, organisation_id: uuid.UUID, job_id: uuid.UUID
) -> None:
    job = get_job(session, organisation_id, job_id)
    if job is not None and job.status == "processing":
        job.status = "indexed"
        job.error = None
        job.completed_at = datetime.now(UTC)
        session.commit()
