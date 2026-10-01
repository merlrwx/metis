import asyncio
import uuid

import pytest
from backend.main import app
from backend.services import jobs
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
