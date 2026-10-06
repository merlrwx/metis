import asyncio
from uuid import UUID

from sqlalchemy import select

from backend.connectors.base import ConnectorError, DocumentChanges, RemoteDocument
from backend.models import Document, DocumentVersion


class UploadSource:
    def __init__(self, session, organisation_id, source_id, storage):
        self.session = session
        self.organisation_id = organisation_id
        self.source_id = source_id
        self.storage = storage

    async def list_documents(self, checkpoint=None):
        rows = self.session.execute(
            select(Document, DocumentVersion)
            .join(
                DocumentVersion,
                (DocumentVersion.id == Document.current_version_id)
                & (DocumentVersion.organisation_id == Document.organisation_id),
            )
            .where(
                Document.organisation_id == self.organisation_id,
                Document.source_id == self.source_id,
            )
        ).all()
        return DocumentChanges(
            [
                RemoteDocument(
                    str(version.id),
                    version.filename,
                    version.mime_type,
                    version.checksum,
                    document.updated_at,
                    document.source_uri,
                )
                for document, version in rows
            ],
            None,
        )

    async def fetch_document(self, document):
        version = self.session.scalar(
            select(DocumentVersion)
            .join(
                Document,
                (Document.id == DocumentVersion.document_id)
                & (Document.organisation_id == DocumentVersion.organisation_id),
            )
            .where(
                DocumentVersion.id == UUID(document.external_id),
                DocumentVersion.organisation_id == self.organisation_id,
                Document.source_id == self.source_id,
            )
        )
        if version is None:
            raise ConnectorError("Upload document was not found in this source")
        return await asyncio.to_thread(self.storage.get, version.object_key)
