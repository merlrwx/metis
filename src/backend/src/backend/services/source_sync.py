import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from backend import database, observability
from backend.connectors.base import ConnectorError, ExpiredCheckpoint
from backend.connectors.microsoft365 import Microsoft365Source
from backend.models import Document, DocumentVersion, IngestionJob, Source
from backend.services import ingestion, jobs
from backend.storage import get_object_storage

LEASE_SECONDS = 900


def owned_source(session, organisation_id, source_id, lock=False):
    statement = select(Source).where(
        Source.organisation_id == organisation_id, Source.id == source_id
    )
    return session.scalar(statement.with_for_update() if lock else statement)


def source_is_busy(source):
    started = source.sync_started_at
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return bool(
        started and started > datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS)
    )


def claim_source(session, organisation_id, source_id):
    source = owned_source(session, organisation_id, source_id, lock=True)
    if source is None or source.type != "microsoft365":
        raise ConnectorError("Microsoft 365 source not found")
    if source_is_busy(source):
        session.rollback()
        return None
    source.sync_started_at = datetime.now(UTC)
    source.sync_status = "syncing"
    source.sync_error = None
    session.commit()
    return source


def external_document(session, organisation_id, source_id, external_id):
    return session.scalar(
        select(Document).where(
            Document.organisation_id == organisation_id,
            Document.source_id == source_id,
            Document.external_id == external_id,
        )
    )


def store_external_document(session, source, remote, data, storage):
    filename = ingestion.clean_filename(remote.name)
    mime_type = ingestion.validate_upload(filename, remote.mime_type, data)
    if (
        not remote.source_url
        or len(remote.source_url) > 2048
        or len(remote.etag) > 1024
    ):
        raise ConnectorError("External document metadata is invalid")
    document = external_document(
        session, source.organisation_id, source.id, remote.external_id
    )
    if document is None:
        document = Document(
            organisation_id=source.organisation_id,
            source_id=source.id,
            external_id=remote.external_id,
            title=filename,
            source_uri=remote.source_url,
        )
        session.add(document)
        session.flush()
    checksum = hashlib.sha256(data).hexdigest()
    version = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.organisation_id == source.organisation_id,
            DocumentVersion.document_id == document.id,
            DocumentVersion.checksum == checksum,
        )
    )
    stored_key = None
    try:
        if version is None:
            version_id = uuid.uuid4()
            stored_key = f"organisations/{source.organisation_id}/documents/{document.id}/versions/{version_id}/original"
            storage.put(stored_key, data, mime_type)
            version = DocumentVersion(
                id=version_id,
                organisation_id=source.organisation_id,
                document_id=document.id,
                filename=filename,
                checksum=checksum,
                object_key=stored_key,
                mime_type=mime_type,
                size_bytes=len(data),
            )
            session.add(version)
            session.flush()
        document.title = filename
        document.source_uri = remote.source_url
        document.external_etag = remote.etag
        document.external_modified_at = remote.modified_at
        document.deleted_at = None
        document.current_version_id = version.id
        job = session.scalar(
            select(IngestionJob).where(
                IngestionJob.organisation_id == source.organisation_id,
                IngestionJob.document_version_id == version.id,
            )
        )
        if job is None:
            job = IngestionJob(
                organisation_id=source.organisation_id,
                document_version_id=version.id,
                status="pending",
            )
            session.add(job)
        source.sync_started_at = datetime.now(UTC)
        session.commit()
        return document, job
    except Exception:
        session.rollback()
        if stored_key is not None:
            storage.delete(stored_key)
        raise


async def synchronize(
    session, organisation_id, source_id, connector, storage, publisher
):
    source = claim_source(session, organisation_id, source_id)
    if source is None:
        return {"queued": 0, "unchanged": 0, "deleted": 0, "busy": True}
    counts = {"queued": 0, "unchanged": 0, "deleted": 0, "busy": False}
    try:
        changes = await connector.list_documents(source.sync_checkpoint)
        for remote in changes.documents:
            document = external_document(
                session, organisation_id, source_id, remote.external_id
            )
            if remote.deleted:
                if document is not None and document.deleted_at is None:
                    document.current_version_id = None
                    document.deleted_at = datetime.now(UTC)
                    counts["deleted"] += 1
                    session.commit()
                continue
            # Unsupported file formats never enter the ingestion pipeline.
            if not any(
                remote.name.lower().endswith(extension)
                for extension in ingestion.MIME_TYPES
            ):
                if document is not None:
                    document.current_version_id = None
                    document.deleted_at = datetime.now(UTC)
                    session.commit()
                continue
            if (
                document is not None
                and document.external_etag == remote.etag
                and document.deleted_at is None
            ):
                pending = session.scalar(
                    select(IngestionJob).where(
                        IngestionJob.organisation_id == organisation_id,
                        IngestionJob.document_version_id == document.current_version_id,
                    )
                )
                if pending is not None and pending.status == "pending":
                    await publisher(str(pending.id), str(organisation_id))
                    jobs.mark_queued(session, organisation_id, pending.id)
                document.title = ingestion.clean_filename(remote.name)
                document.source_uri = remote.source_url or document.source_uri
                document.external_modified_at = (
                    remote.modified_at or document.external_modified_at
                )
                session.commit()
                counts["unchanged"] += 1
                continue
            data = await connector.fetch_document(remote)
            document, job = store_external_document(
                session, source, remote, data, storage
            )
            if job.status == "pending":
                await publisher(str(job.id), str(organisation_id))
                jobs.mark_queued(session, organisation_id, job.id)
                counts["queued"] += 1
            else:
                counts["unchanged"] += 1
        if changes.full_snapshot:
            seen = {remote.external_id for remote in changes.documents}
            for document in session.scalars(
                select(Document).where(
                    Document.organisation_id == organisation_id,
                    Document.source_id == source_id,
                    Document.external_id.is_not(None),
                    Document.deleted_at.is_(None),
                )
            ):
                if document.external_id not in seen:
                    document.current_version_id = None
                    document.deleted_at = datetime.now(UTC)
                    counts["deleted"] += 1
        source.sync_checkpoint = changes.checkpoint
        source.last_synced_at = datetime.now(UTC)
        source.sync_started_at = None
        source.sync_status = "idle"
        source.sync_error = None
        session.commit()
        observability.log_event(
            "source_synchronized",
            source_id=str(source_id),
            organisation_id=str(organisation_id),
            **counts,
        )
        return counts
    except Exception as error:
        session.rollback()
        source = owned_source(session, organisation_id, source_id)
        source.sync_status = "failed"
        source.sync_started_at = None
        source.sync_error = type(error).__name__
        if isinstance(error, ExpiredCheckpoint):
            source.sync_checkpoint = None
        session.commit()
        observability.log_event(
            "source_sync_failed",
            source_id=str(source_id),
            organisation_id=str(organisation_id),
            error_type=type(error).__name__,
        )
        raise


async def sync_source(organisation_id, source_id, publisher):
    with database.SessionLocal() as session:
        source = owned_source(session, organisation_id, source_id)
        if source is None or source.type != "microsoft365":
            raise ConnectorError("Microsoft 365 source not found")
        try:
            connector = Microsoft365Source(source.id)
            storage = get_object_storage()
        except Exception as error:
            source.sync_status = "failed"
            source.sync_error = type(error).__name__
            source.sync_started_at = None
            session.commit()
            raise
        return await synchronize(
            session,
            organisation_id,
            source_id,
            connector,
            storage,
            publisher,
        )
