from unittest.mock import AsyncMock

from backend.storage import LocalObjectStorage
from backend_test_client import new_authenticated_client

from backend import main


def configure_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "QUEUE_CONFIGURED", True)
    monkeypatch.setattr(main.process_ingestion_job, "kiq", AsyncMock())
    monkeypatch.setattr(
        main, "get_object_storage", lambda: LocalObjectStorage(tmp_path)
    )
    return new_authenticated_client(main.app)


def test_upload_returns_tenant_scoped_job_and_reuses_duplicate(monkeypatch, tmp_path):
    client = configure_upload(monkeypatch, tmp_path)
    organisation = client.post(
        "/api/organisations", json={"name": "Upload tenant"}
    ).json()
    path = f"/api/organisations/{organisation['id']}/documents/upload"
    payload = {"file": ("policy.txt", b"Keep the records.", "text/plain")}

    uploaded = client.post(path, files=payload)
    assert uploaded.status_code == 202
    first = uploaded.json()
    assert first["job"]["status"] == "queued"
    assert first["document"]["ingestion_status"] == "queued"
    assert (
        first["document"]["current_version_id"] == first["job"]["document_version_id"]
    )
    assert len(list(tmp_path.rglob("original"))) == 1

    duplicate = client.post(path, files=payload)
    assert duplicate.status_code == 202
    assert duplicate.json()["job"]["id"] == first["job"]["id"]
    assert len(list(tmp_path.rglob("original"))) == 1

    updated = client.post(
        path,
        files={"file": ("policy.txt", b"Keep the latest records.", "text/plain")},
    )
    assert updated.status_code == 202
    latest = updated.json()
    assert latest["document"]["id"] == first["document"]["id"]
    assert (
        latest["document"]["current_version_id"]
        != first["document"]["current_version_id"]
    )
    assert latest["job"]["id"] != first["job"]["id"]
    assert len(list(tmp_path.rglob("original"))) == 2

    status = client.get(
        f"/api/organisations/{organisation['id']}/documents/{first['document']['id']}"
    )
    assert status.status_code == 200
    assert status.json()["ingestion_status"] == "queued"

    other = client.post(
        "/api/organisations", json={"name": "Other upload tenant"}
    ).json()
    cross_tenant = client.get(
        f"/api/organisations/{other['id']}/documents/{first['document']['id']}"
    )
    assert cross_tenant.status_code == 404


def test_upload_rejects_cross_tenant_source_and_invalid_mime(monkeypatch, tmp_path):
    client = configure_upload(monkeypatch, tmp_path)
    first = client.post("/api/organisations", json={"name": "First"}).json()
    second = client.post("/api/organisations", json={"name": "Second"}).json()
    source = client.post(
        f"/api/organisations/{second['id']}/sources",
        json={"name": "Private uploads"},
    ).json()
    path = f"/api/organisations/{first['id']}/documents/upload"

    cross_tenant = client.post(
        path,
        data={"source_id": source["id"]},
        files={"file": ("policy.txt", b"Keep the records.", "text/plain")},
    )
    assert cross_tenant.status_code == 404

    invalid_mime = client.post(
        path,
        files={"file": ("policy.pdf", b"not a pdf", "text/plain")},
    )
    assert invalid_mime.status_code == 415


def test_original_download_requires_matching_organisation_document_and_version(
    monkeypatch, tmp_path
):
    client = configure_upload(monkeypatch, tmp_path)
    org = client.post("/api/organisations", json={"name": "Downloads"}).json()["id"]
    other = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
    upload = client.post(
        f"/api/organisations/{org}/documents/upload",
        files={"file": ("policy.txt", b"Synthetic private original", "text/plain")},
    ).json()
    document = upload["document"]
    path = f"/documents/{document['id']}/versions/{document['current_version_id']}/download"
    response = client.get(f"/api/organisations/{org}{path}")
    assert response.status_code == 200
    assert response.content == b"Synthetic private original"
    assert response.headers["cache-control"] == "private, no-store"
    assert "attachment" in response.headers["content-disposition"]
    assert client.get(f"/api/organisations/{other}{path}").status_code == 404
    from datetime import UTC, datetime
    from uuid import UUID

    from backend.models import Document

    from backend import database

    with database.SessionLocal() as session:
        session.get(Document, UUID(document["id"])).deleted_at = datetime.now(UTC)
        session.commit()
    assert client.get(f"/api/organisations/{org}{path}").status_code == 404
