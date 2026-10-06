from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RemoteDocument:
    external_id: str
    name: str
    mime_type: str
    etag: str
    modified_at: datetime | None
    source_url: str | None
    deleted: bool = False


@dataclass(frozen=True)
class DocumentChanges:
    documents: list[RemoteDocument]
    checkpoint: str | None
    full_snapshot: bool = False


class KnowledgeSource(Protocol):
    async def list_documents(
        self, checkpoint: str | None = None
    ) -> DocumentChanges: ...
    async def fetch_document(self, document: RemoteDocument) -> bytes: ...


class ConnectorError(RuntimeError):
    pass


class ExpiredCheckpoint(ConnectorError):
    pass
