import uuid
from unittest.mock import AsyncMock

from backend.main import app
from backend.models import Document, DocumentVersion, IngestionJob
from backend.services import jobs
from backend_test_client import authenticated_client, new_authenticated_client
from sqlalchemy import func, select

from backend import database
from backend import main as main_module


def test_idempotency_key_is_stable_and_organisation_scoped():
    with authenticated_client(app) as client:
        first = client.post("/api/organisations", json={"name": "First"}).json()
        second = client.post("/api/organisations", json={"name": "Second"}).json()
    with database.SessionLocal() as session:
        first_job, created = jobs.create_test_job(
            session, uuid.UUID(first["id"]), "same-request"
        )
        repeated_job, repeated_created = jobs.create_test_job(
            session, uuid.UUID(first["id"]), "same-request"
        )
        other_job, other_created = jobs.create_test_job(
            session, uuid.UUID(second["id"]), "same-request"
        )

    assert created is True
    assert repeated_created is False
    assert first_job.id == repeated_job.id
    assert other_created is True
    assert other_job.id != first_job.id


def test_job_status_is_organisation_scoped():
    with authenticated_client(app) as client:
        first = client.post("/api/organisations", json={"name": "First"}).json()
        second = client.post("/api/organisations", json={"name": "Second"}).json()
    with database.SessionLocal() as session:
        job, _ = jobs.create_test_job(session, uuid.UUID(first["id"]), None)

    with authenticated_client(app) as client:
        response = client.get(f"/api/organisations/{second['id']}/jobs/{job.id}")

    assert response.status_code == 404


def test_job_submission_requires_a_configured_queue(monkeypatch):
    monkeypatch.setattr(main_module, "QUEUE_CONFIGURED", False)
    with authenticated_client(app) as client:
        organisation = client.post(
            "/api/organisations", json={"name": "Queue unavailable"}
        ).json()
        response = client.post(
            "/api/jobs/test", json={"organisation_id": organisation["id"]}
        )

    with database.SessionLocal() as session:
        job_count = session.scalar(
            select(func.count())
            .select_from(IngestionJob)
            .where(IngestionJob.organisation_id == uuid.UUID(organisation["id"]))
        )

    assert response.status_code == 503
    assert job_count == 0


def test_publish_failure_leaves_a_retryable_job(monkeypatch):
    async def no_op():
        return None

    async def fail_publish(*_args, **_kwargs):
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr(main_module, "QUEUE_CONFIGURED", True)
    monkeypatch.setattr(main_module.broker, "startup", no_op)
    monkeypatch.setattr(main_module.broker, "shutdown", no_op)
    monkeypatch.setattr(main_module.process_ingestion_job, "kiq", fail_publish)

    with authenticated_client(app) as client:
        organisation = client.post(
            "/api/organisations", json={"name": "Queue outage"}
        ).json()
        response = client.post(
            "/api/jobs/test",
            json={"organisation_id": organisation["id"]},
            headers={"Idempotency-Key": "retry-later"},
        )

    with database.SessionLocal() as session:
        job = session.scalar(
            select(IngestionJob).where(
                IngestionJob.organisation_id == uuid.UUID(organisation["id"])
            )
        )

    assert response.status_code == 503
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.error == "Unable to publish job: ConnectionError"


def test_failed_document_ingestion_can_be_retried(monkeypatch):
    monkeypatch.setattr(main_module, "QUEUE_CONFIGURED", True)
    monkeypatch.setattr(main_module.process_ingestion_job, "kiq", AsyncMock())

    client = new_authenticated_client(app)
    organisation = client.post(
        "/api/organisations", json={"name": "Retry clinic"}
    ).json()
    other_organisation = client.post(
        "/api/organisations", json={"name": "Other clinic"}
    ).json()
    created = client.post(
        f"/api/organisations/{organisation['id']}/documents",
        json={"title": "Policy"},
    ).json()
    organisation_id = uuid.UUID(organisation["id"])
    document_id = uuid.UUID(created["id"])

    with database.SessionLocal() as session:
        document = session.get(Document, document_id)
        version = DocumentVersion(
            organisation_id=organisation_id,
            document_id=document_id,
            filename="policy.txt",
            checksum="a" * 64,
            object_key=f"organisations/{organisation_id}/policy.txt",
            mime_type="text/plain",
            size_bytes=1,
        )
        session.add(version)
        session.flush()
        document.current_version_id = version.id
        job = IngestionJob(
            organisation_id=organisation_id,
            document_version_id=version.id,
            status="failed",
            attempts=3,
            error="Text extraction failed",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    cross_tenant = client.post(
        f"/api/organisations/{other_organisation['id']}/documents/{document_id}/retry"
    )
    retried = client.post(
        f"/api/organisations/{organisation['id']}/documents/{document_id}/retry"
    )
    repeated = client.post(
        f"/api/organisations/{organisation['id']}/documents/{document_id}/retry"
    )

    assert cross_tenant.status_code == 404
    assert retried.status_code == 202
    assert retried.json()["id"] == str(job_id)
    assert retried.json()["status"] == "queued"
    assert retried.json()["attempts"] == 0
    assert repeated.status_code == 409
