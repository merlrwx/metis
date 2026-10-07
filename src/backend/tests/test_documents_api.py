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


def test_document_detail_is_bounded_and_tenant_scoped(monkeypatch, tmp_path):
    client = configure_upload(monkeypatch, tmp_path)
    org = client.post("/api/organisations", json={"name": "Details"}).json()["id"]
    other = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
    upload = client.post(
        f"/api/organisations/{org}/documents/upload",
        files={"file": ("pay.csv", b"Period,Net AUD\nJanuary,2000.10", "text/csv")},
    ).json()
    document = upload["document"]["id"]
    path = f"/api/organisations/{org}/documents/{document}/detail"
    response = client.get(path)
    assert response.status_code == 200
    detail = response.json()
    assert detail["extraction_preview"] == ""
    assert detail["chunk_count"] == 0
    assert detail["last_successful_indexing"] is None
    assert "all members" in detail["visibility"]
    assert client.get(path.replace(org, other)).status_code == 404
    from uuid import UUID

    from backend.models import DocumentVersion

    from backend import database

    with database.SessionLocal() as session:
        version = session.get(
            DocumentVersion, UUID(upload["document"]["current_version_id"])
        )
        version.extracted_text = "x" * 5000
        version.extraction_metadata = {"pages": [{"page": 1, "start": 0, "end": 5000}]}
        session.commit()
    detail = client.get(path).json()
    assert len(detail["extraction_preview"]) == 4000
    assert detail["preview_truncated"] is True
    assert detail["page_count"] == 1
    assert detail["versions"][0]["current"] is True
    assert "object_key" not in detail["versions"][0]


def test_document_rename_keeps_original_identity_and_checks_scope(
    monkeypatch, tmp_path
):
    client = configure_upload(monkeypatch, tmp_path)
    org = client.post("/api/organisations", json={"name": "Rename"}).json()["id"]
    other = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
    original = client.post(
        f"/api/organisations/{org}/documents/upload",
        files={"file": ("pay.txt", b"January net AUD 2000.10", "text/plain")},
    ).json()["document"]
    path = f"/api/organisations/{org}/documents/{original['id']}"
    assert (
        client.patch(path.replace(org, other), json={"title": "Private"}).status_code
        == 404
    )
    assert client.patch(path, json={"title": "   "}).status_code == 422
    renamed = client.patch(path, json={"title": " January payslip "})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "January payslip"
    assert renamed.json()["source_uri"] == original["source_uri"]
    assert renamed.json()["current_version_id"] == original["current_version_id"]


def test_upload_explicit_conflict_modes_preserve_unrelated_documents(
    monkeypatch, tmp_path
):
    client = configure_upload(monkeypatch, tmp_path)
    org = client.post("/api/organisations", json={"name": "Collisions"}).json()["id"]
    path = f"/api/organisations/{org}/documents/upload"
    files = {"file": ("pay.txt", b"January net AUD 2000.10", "text/plain")}
    original = client.post(path, files=files).json()["document"]
    assert (
        client.post(
            path,
            files={"file": ("pay.txt", b"Different payslip", "text/plain")},
            data={"conflict_action": "reject"},
        ).status_code
        == 409
    )
    unrelated = client.post(path, files=files, data={"conflict_action": "new"})
    assert unrelated.status_code == 202
    assert unrelated.json()["document"]["id"] != original["id"]
    replaced = client.post(
        path,
        files={"file": ("pay.txt", b"February net AUD 2100.25", "text/plain")},
        data={"conflict_action": "replace"},
    )
    assert replaced.status_code == 202
    assert replaced.json()["document"]["id"] == original["id"]
    assert (
        replaced.json()["document"]["current_version_id"]
        != original["current_version_id"]
    )
    assert (
        client.post(path, files=files, data={"conflict_action": "unknown"}).status_code
        == 422
    )


def test_document_removal_excludes_previews_downloads_and_rename_but_retains_original(
    monkeypatch, tmp_path
):
    client = configure_upload(monkeypatch, tmp_path)
    org = client.post("/api/organisations", json={"name": "Removal"}).json()["id"]
    other = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
    original = client.post(
        f"/api/organisations/{org}/documents/upload",
        files={"file": ("pay.txt", b"January net AUD 2000.10", "text/plain")},
    ).json()["document"]
    path = f"/api/organisations/{org}/documents/{original['id']}"
    assert client.delete(path.replace(org, other)).status_code == 404
    removed = client.delete(path)
    assert removed.status_code == 200
    assert "not permanent erasure" in removed.json()["retention"]
    assert client.get(path + "/detail").status_code == 404
    assert client.patch(path, json={"title": "Still here"}).status_code == 404
    assert (
        client.get(
            path + f"/versions/{original['current_version_id']}/download"
        ).status_code
        == 404
    )
    assert len(list(tmp_path.rglob("original"))) == 1
    assert client.get(path).json()["ingestion_status"] == "deleted"


def test_reindex_requires_owned_active_completed_document(monkeypatch, tmp_path):
    from uuid import UUID

    from backend.models import IngestionJob

    from backend import database

    client = configure_upload(monkeypatch, tmp_path)
    org = client.post("/api/organisations", json={"name": "Reindex"}).json()["id"]
    other = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
    uploaded = client.post(
        f"/api/organisations/{org}/documents/upload",
        files={"file": ("pay.txt", b"January net AUD 2000.10", "text/plain")},
    ).json()
    path = f"/api/organisations/{org}/documents/{uploaded['document']['id']}"
    assert client.post(path.replace(org, other) + "/reindex").status_code == 404
    assert client.post(path + "/reindex").status_code == 409
    with database.SessionLocal() as session:
        job = session.get(IngestionJob, UUID(uploaded["job"]["id"]))
        job.status = "indexed"
        session.commit()
    reindexed = client.post(path + "/reindex")
    assert reindexed.status_code == 202
    assert reindexed.json()["id"] == uploaded["job"]["id"]
    assert reindexed.json()["status"] == "queued"
    client.delete(path)
    assert client.post(path + "/reindex").status_code == 404
    assert client.post(path + "/retry").status_code == 404


def test_targeted_replacement_accepts_new_filename_and_rejects_foreign_document(
    monkeypatch, tmp_path
):
    client = configure_upload(monkeypatch, tmp_path)
    org = client.post("/api/organisations", json={"name": "Target"}).json()["id"]
    other = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
    path = f"/api/organisations/{org}/documents/upload"
    original = client.post(
        path, files={"file": ("pay.txt", b"January net AUD 2000.10", "text/plain")}
    ).json()["document"]
    files = {"file": ("revised-pay.txt", b"January net AUD 2100.25", "text/plain")}
    data = {"replace_document_id": original["id"], "conflict_action": "replace"}
    assert (
        client.post(path.replace(org, other), files=files, data=data).status_code == 404
    )
    replacement = client.post(path, files=files, data=data)
    assert replacement.status_code == 202
    assert replacement.json()["document"]["id"] == original["id"]
    assert (
        replacement.json()["document"]["current_version_id"]
        != original["current_version_id"]
    )
    unchanged = client.post(path, files=files, data=data)
    assert unchanged.json()["job"]["id"] == replacement.json()["job"]["id"]
