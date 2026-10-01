import asyncio
import uuid

import pytest
from backend.main import app
from backend.models import Document, DocumentVersion
from backend.services import documents as document_service
from backend.services import jobs
from backend.storage import LocalObjectStorage
from backend.tasks import run_ingestion_job
from fastapi.testclient import TestClient

from backend import database


def make_job(name):
    with TestClient(app) as client:
        organisation = client.post("/api/organisations", json={"name": name}).json()
    organisation_id = uuid.UUID(organisation["id"])
    with database.SessionLocal() as session:
        job, _ = jobs.create_test_job(session, organisation_id, None)
        job_id = job.id
        jobs.mark_queued(session, organisation_id, job_id)
    return organisation_id, job_id


def test_job_handler_retries_then_completes(monkeypatch):
    monkeypatch.setenv("METIS_TEST_JOB_DELAY_SECONDS", "0")
    monkeypatch.setenv("METIS_TEST_JOB_FAIL_ATTEMPTS", "1")
    organisation_id, job_id = make_job("Retry once")

    with pytest.raises(RuntimeError, match="simulated transient worker failure"):
        asyncio.run(run_ingestion_job(str(job_id), str(organisation_id)))
    with database.SessionLocal() as session:
        queued = jobs.get_job(session, organisation_id, job_id)
        assert queued.status == "queued"
        assert queued.attempts == 1
        assert queued.error == "simulated transient worker failure"

    asyncio.run(run_ingestion_job(str(job_id), str(organisation_id)))
    with database.SessionLocal() as session:
        completed = jobs.get_job(session, organisation_id, job_id)
        assert completed.status == "completed"
        assert completed.attempts == 2
        assert completed.error is None


def test_job_handler_records_terminal_error_and_ignores_duplicate(monkeypatch):
    monkeypatch.setenv("METIS_TEST_JOB_DELAY_SECONDS", "0")
    monkeypatch.setenv("METIS_TEST_JOB_FAIL_ATTEMPTS", "10")
    organisation_id, job_id = make_job("Always fails")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="simulated transient worker failure"):
            asyncio.run(run_ingestion_job(str(job_id), str(organisation_id)))
    asyncio.run(run_ingestion_job(str(job_id), str(organisation_id)))
    asyncio.run(run_ingestion_job(str(job_id), str(organisation_id)))

    with database.SessionLocal() as session:
        failed = jobs.get_job(session, organisation_id, job_id)
        assert failed.status == "failed"
        assert failed.attempts == 3
        assert failed.error == "simulated transient worker failure"


def test_document_job_extracts_text_and_marks_version_indexed(
    monkeypatch, tmp_path, sample_pdf
):
    monkeypatch.setenv("OBJECT_STORAGE_BACKEND", "local")
    monkeypatch.setenv("OBJECT_STORAGE_LOCAL_DIR", str(tmp_path))
    with TestClient(app) as client:
        organisation = client.post(
            "/api/organisations", json={"name": "Extract document"}
        ).json()
    organisation_id = uuid.UUID(organisation["id"])
    storage = LocalObjectStorage(tmp_path)
    with database.SessionLocal() as session:
        document, job, _ = document_service.upload_document(
            session,
            organisation_id,
            "policy.pdf",
            "application/pdf",
            sample_pdf,
            storage,
        )
        jobs.mark_queued(session, organisation_id, job.id)
        document_id = document.id
        job_id = job.id

    asyncio.run(run_ingestion_job(str(job_id), str(organisation_id)))

    with database.SessionLocal() as session:
        indexed = jobs.get_job(session, organisation_id, job_id)
        document = session.get(Document, document_id)
        version = session.get(DocumentVersion, document.current_version_id)
        assert indexed.status == "indexed"
        assert indexed.attempts == 1
        assert version.extracted_text == "Medication incident policy"
        assert version.extraction_metadata["pages"] == [
            {"page": 1, "start": 0, "end": 26}
        ]


def test_document_worker_does_not_read_another_organisations_job(tmp_path, sample_pdf):
    client = TestClient(app)
    organisation = client.post(
        "/api/organisations", json={"name": "Document owner"}
    ).json()
    other_organisation = client.post(
        "/api/organisations", json={"name": "Different tenant"}
    ).json()
    organisation_id = uuid.UUID(organisation["id"])
    other_organisation_id = uuid.UUID(other_organisation["id"])
    with database.SessionLocal() as session:
        document, job, _ = document_service.upload_document(
            session,
            organisation_id,
            "policy.pdf",
            "application/pdf",
            sample_pdf,
            LocalObjectStorage(tmp_path),
        )
        jobs.mark_queued(session, organisation_id, job.id)
        job_id = job.id
        version_id = document.current_version_id

    asyncio.run(run_ingestion_job(str(job_id), str(other_organisation_id)))

    with database.SessionLocal() as session:
        job = jobs.get_job(session, organisation_id, job_id)
        version = session.get(DocumentVersion, version_id)
        assert job.status == "queued"
        assert job.attempts == 0
        assert version.extracted_text is None
