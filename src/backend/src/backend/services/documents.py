import hashlib
import uuid
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    Document,
    DocumentVersion,
    IngestionJob,
    Organisation,
    Source,
)
from backend.storage import ObjectStorage


def upload_document(
    session: Session,
    organisation_id: uuid.UUID,
    filename: str,
    mime_type: str,
    data: bytes,
    storage: ObjectStorage,
    source_id: uuid.UUID | None = None,
) -> tuple[Document | None, IngestionJob | None, bool]:
    if session.get(Organisation, organisation_id) is None:
        return None, None, False

    if source_id is None:
        source = session.scalar(
            select(Source).where(
                Source.organisation_id == organisation_id,
                Source.type == "upload",
                Source.name == "File uploads",
            )
        )
        if source is None:
            source = Source(
                organisation_id=organisation_id,
                type="upload",
                name="File uploads",
                configuration={},
            )
            session.add(source)
            session.flush()
    else:
        source = session.scalar(
            select(Source).where(
                Source.id == source_id,
                Source.organisation_id == organisation_id,
            )
        )
        if source is None or source.type != "upload":
            return None, None, False

    source_uri = f"upload://{source.id}/{quote(filename, safe='')}"
    document = session.scalar(
        select(Document).where(
            Document.organisation_id == organisation_id,
            Document.source_uri == source_uri,
        )
    )
    if document is None:
        document = Document(
            organisation_id=organisation_id,
            source_id=source.id,
            title=filename,
            source_uri=source_uri,
        )
        session.add(document)
        session.flush()

    checksum = hashlib.sha256(data).hexdigest()
    version = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.organisation_id == organisation_id,
            DocumentVersion.document_id == document.id,
            DocumentVersion.checksum == checksum,
        )
    )
    stored_key = None
    try:
        created = version is None
        if created:
            version_id = uuid.uuid4()
            stored_key = (
                f"organisations/{organisation_id}/documents/{document.id}"
                f"/versions/{version_id}/original"
            )
            storage.put(stored_key, data, mime_type)
            version = DocumentVersion(
                id=version_id,
                organisation_id=organisation_id,
                document_id=document.id,
                filename=filename,
                checksum=checksum,
                object_key=stored_key,
                mime_type=mime_type,
                size_bytes=len(data),
            )
            session.add(version)
            document.current_version_id = version.id
        else:
            document.current_version_id = version.id

        job = session.scalar(
            select(IngestionJob).where(
                IngestionJob.organisation_id == organisation_id,
                IngestionJob.document_version_id == version.id,
            )
        )
        if job is None:
            job = IngestionJob(
                organisation_id=organisation_id,
                document_version_id=version.id,
                status="pending",
            )
            session.add(job)

        session.commit()
        session.refresh(document)
        session.refresh(job)
        return document, job, created
    except Exception:
        session.rollback()
        if stored_key is not None:
            storage.delete(stored_key)
        raise


def get_document_job(
    session: Session, organisation_id: uuid.UUID, version_id: uuid.UUID | None
) -> IngestionJob | None:
    if version_id is None:
        return None
    return session.scalar(
        select(IngestionJob).where(
            IngestionJob.organisation_id == organisation_id,
            IngestionJob.document_version_id == version_id,
        )
    )
