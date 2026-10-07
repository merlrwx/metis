from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from backend.connectors.base import (
    ConnectorError,
    DocumentChanges,
    ExpiredCheckpoint,
    RemoteDocument,
)
from backend.connectors.upload import UploadSource
from backend.models import Document, DocumentVersion, IngestionJob, Organisation, Source
from backend.services.documents import upload_document
from backend.services.source_sync import sync_source, synchronize
from backend.storage import LocalObjectStorage
from backend.tasks import run_ingestion_job
from sqlalchemy import func, select

from backend import database


class FixtureSource:
    def __init__(
        self, remote, content=b"After a medication incident notify the supervisor."
    ):
        self.changes = DocumentChanges([remote], "checkpoint-1")
        self.content = content
        self.downloads = 0

    async def list_documents(self, checkpoint=None):
        return self.changes

    async def fetch_document(self, document):
        self.downloads += 1
        return self.content


def remote_document():
    return RemoteDocument(
        "file-123",
        "policy.txt",
        "text/plain",
        "etag-1",
        datetime.now(UTC),
        "https://example.sharepoint.com/policy.txt",
    )


def add_source(session, name="A"):
    organisation = Organisation(name=name)
    session.add(organisation)
    session.flush()
    source = Source(
        organisation_id=organisation.id,
        type="microsoft365",
        name="Policies",
        configuration={"sync_enabled": True},
    )
    session.add(source)
    session.commit()
    return organisation, source


@pytest.mark.asyncio
async def test_changed_unchanged_deleted_documents_keep_versions_and_tenant_scope(
    tmp_path,
    monkeypatch,
):
    storage = LocalObjectStorage(tmp_path)
    monkeypatch.setenv("OBJECT_STORAGE_LOCAL_DIR", str(tmp_path))
    publisher = AsyncMock()
    with database.SessionLocal() as session:
        tenant_a, source_a = add_source(session)
        tenant_b, source_b = add_source(session, "B")
        remote = remote_document()
        connector = FixtureSource(remote)
        first = await synchronize(
            session, tenant_a.id, source_a.id, connector, storage, publisher
        )
        assert first["queued"] == 1
        job_id, organisation_id = publisher.call_args.args
        assert organisation_id == str(tenant_a.id)
        await run_ingestion_job(job_id, organisation_id)
        document = session.scalar(
            select(Document).where(Document.organisation_id == tenant_a.id)
        )
        assert document.source_uri == remote.source_url
        assert document.external_modified_at is not None
        assert source_a.sync_checkpoint == "checkpoint-1"
        again = await synchronize(
            session, tenant_a.id, source_a.id, connector, storage, publisher
        )
        assert again["unchanged"] == 1 and connector.downloads == 1
        assert publisher.await_count == 1
        connector.changes = DocumentChanges(
            [replace(remote, name="renamed-policy.txt", etag="metadata-change")],
            "metadata-cursor",
        )
        metadata_only = await synchronize(
            session, tenant_a.id, source_a.id, connector, storage, publisher
        )
        assert metadata_only["unchanged"] == 1 and metadata_only["queued"] == 0
        assert document.title == "renamed-policy.txt"
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 1
        connector.changes = DocumentChanges(
            [replace(remote, etag="etag-2")], "checkpoint-2"
        )
        connector.content = (
            b"After a medication incident notify the supervisor and record the event."
        )
        await synchronize(
            session, tenant_a.id, source_a.id, connector, storage, publisher
        )
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 2
        assert document.external_etag == "etag-2"
        other = FixtureSource(
            replace(remote, source_url="https://example.sharepoint.com/tenant-b.txt")
        )
        await synchronize(session, tenant_b.id, source_b.id, other, storage, publisher)
        foreign = session.scalar(
            select(Document).where(Document.organisation_id == tenant_b.id)
        )
        connector.changes = DocumentChanges(
            [replace(remote, deleted=True)], "checkpoint-3"
        )
        await synchronize(
            session, tenant_a.id, source_a.id, connector, storage, publisher
        )
        assert document.deleted_at is not None and document.current_version_id is None
        assert foreign.deleted_at is None and foreign.current_version_id is not None
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 3


@pytest.mark.asyncio
async def test_unsupported_rename_with_same_marker_withdraws_document(tmp_path):
    with database.SessionLocal() as session:
        tenant, source = add_source(session)
        remote = remote_document()
        connector = FixtureSource(remote)
        storage = LocalObjectStorage(tmp_path)
        publisher = AsyncMock()
        await synchronize(session, tenant.id, source.id, connector, storage, publisher)
        connector.changes = DocumentChanges(
            [replace(remote, name="policy.exe")], "renamed-cursor"
        )
        await synchronize(session, tenant.id, source.id, connector, storage, publisher)
        document = session.scalar(select(Document))
        assert document.current_version_id is None and document.deleted_at is not None
        assert connector.downloads == 1
        assert publisher.await_count == 1


@pytest.mark.asyncio
async def test_queue_failure_retries_pending_version_without_downloading_again(
    tmp_path,
):
    with database.SessionLocal() as session:
        tenant, source = add_source(session)
        connector = FixtureSource(remote_document())
        with pytest.raises(RuntimeError):
            await synchronize(
                session,
                tenant.id,
                source.id,
                connector,
                LocalObjectStorage(tmp_path),
                AsyncMock(side_effect=RuntimeError("queue unavailable")),
            )
        assert source.sync_status == "failed" and source.sync_checkpoint is None
        assert source.sync_error == "RuntimeError"
        assert session.scalar(select(IngestionJob)).status == "pending"
        publisher = AsyncMock()
        await synchronize(
            session,
            tenant.id,
            source.id,
            connector,
            LocalObjectStorage(tmp_path),
            publisher,
        )
        assert connector.downloads == 1
        publisher.assert_awaited_once()
        assert session.scalar(select(IngestionJob)).status == "queued"
        assert source.sync_status == "idle"


@pytest.mark.asyncio
async def test_expired_cursor_recovers_with_scoped_full_snapshot_and_busy_lease(
    tmp_path,
):
    with database.SessionLocal() as session:
        tenant, source = add_source(session)
        connector = FixtureSource(remote_document())
        await synchronize(
            session,
            tenant.id,
            source.id,
            connector,
            LocalObjectStorage(tmp_path),
            AsyncMock(),
        )
        connector.list_documents = AsyncMock(side_effect=ExpiredCheckpoint("expired"))
        with pytest.raises(ExpiredCheckpoint):
            await synchronize(
                session,
                tenant.id,
                source.id,
                connector,
                LocalObjectStorage(tmp_path),
                AsyncMock(),
            )
        assert source.sync_checkpoint is None
        connector.list_documents = AsyncMock(
            return_value=DocumentChanges([], "new-cursor", full_snapshot=True)
        )
        source.sync_started_at = datetime.now(UTC)
        session.commit()
        assert (
            await synchronize(
                session,
                tenant.id,
                source.id,
                connector,
                LocalObjectStorage(tmp_path),
                AsyncMock(),
            )
        )["busy"] is True
        connector.list_documents.assert_not_awaited()
        source.sync_started_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()
        result = await synchronize(
            session,
            tenant.id,
            source.id,
            connector,
            LocalObjectStorage(tmp_path),
            AsyncMock(),
        )
        assert result["deleted"] == 1
        assert session.scalar(select(Document)).current_version_id is None


@pytest.mark.asyncio
async def test_foreign_source_is_rejected_before_credentials_or_download(
    tmp_path, monkeypatch
):
    with database.SessionLocal() as session:
        tenant_a, source_a = add_source(session)
        tenant_b, _ = add_source(session, "B")
        connector = FixtureSource(remote_document())
        connector.list_documents = AsyncMock()
        with pytest.raises(ConnectorError):
            await synchronize(
                session,
                tenant_b.id,
                source_a.id,
                connector,
                LocalObjectStorage(tmp_path),
                AsyncMock(),
            )
        connector.list_documents.assert_not_awaited()
        with pytest.raises(ConnectorError):
            await sync_source(tenant_b.id, source_a.id, AsyncMock())
        with pytest.raises(ConnectorError):
            await sync_source(tenant_a.id, source_a.id, AsyncMock())
        session.refresh(source_a)
        assert (
            source_a.sync_status == "failed" and source_a.sync_error == "ConnectorError"
        )


@pytest.mark.asyncio
async def test_upload_adapter_scopes_listing_and_download_to_owned_source(tmp_path):
    storage = LocalObjectStorage(tmp_path)
    with database.SessionLocal() as session:
        tenant, _ = add_source(session)
        document, _job, _ = upload_document(
            session, tenant.id, "file.txt", "text/plain", b"Uploaded policy", storage
        )
        adapter = UploadSource(session, tenant.id, document.source_id, storage)
        changes = await adapter.list_documents()
        assert len(changes.documents) == 1
        assert await adapter.fetch_document(changes.documents[0]) == b"Uploaded policy"
        foreign = UploadSource(session, uuid4(), document.source_id, storage)
        assert (await foreign.list_documents()).documents == []
        with pytest.raises(ConnectorError):
            await foreign.fetch_document(changes.documents[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_after_download", [False, True])
async def test_interrupted_sync_does_not_restore_disconnected_content(
    tmp_path, stop_after_download
):
    publisher = AsyncMock()
    with database.SessionLocal() as session:
        organisation, source = add_source(session)
        source_id = source.id

        def disconnect():
            with database.SessionLocal() as concurrent:
                current = concurrent.get(Source, source_id)
                current.configuration = {
                    "disconnected": True,
                    "sync_paused": True,
                    "sync_enabled": False,
                }
                current.sync_status = "disconnected"
                current.sync_started_at = None
                concurrent.commit()

        class Interrupted(FixtureSource):
            async def list_documents(self, checkpoint=None):
                result = await super().list_documents(checkpoint)
                if not stop_after_download:
                    disconnect()
                return result

            async def fetch_document(self, document):
                result = await super().fetch_document(document)
                disconnect()
                return result

        connector = Interrupted(remote_document())
        result = await synchronize(
            session,
            organisation.id,
            source_id,
            connector,
            LocalObjectStorage(tmp_path),
            publisher,
        )
        assert result["stopped"] is True
        assert result["queued"] == 0
        publisher.assert_not_awaited()
        assert session.scalar(select(func.count(Document.id))) == 0
        session.refresh(source)
        assert source.sync_status == "disconnected"
        assert source.sync_checkpoint is None
        assert list(tmp_path.rglob("original")) == []
