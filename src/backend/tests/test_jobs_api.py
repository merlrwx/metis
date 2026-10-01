import uuid

from backend.main import app
from backend.models import IngestionJob
from backend.services import jobs
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import database
from backend import main as main_module


def test_idempotency_key_is_stable_and_organisation_scoped():
    with TestClient(app) as client:
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
    with TestClient(app) as client:
        first = client.post("/api/organisations", json={"name": "First"}).json()
        second = client.post("/api/organisations", json={"name": "Second"}).json()
    with database.SessionLocal() as session:
        job, _ = jobs.create_test_job(session, uuid.UUID(first["id"]), None)

    with TestClient(app) as client:
        response = client.get(f"/api/organisations/{second['id']}/jobs/{job.id}")

    assert response.status_code == 404


def test_job_submission_requires_a_configured_queue(monkeypatch):
    monkeypatch.setattr(main_module, "QUEUE_CONFIGURED", False)
    with TestClient(app) as client:
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

    with TestClient(app) as client:
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
