import uuid

from backend.main import app
from backend.services import jobs
from backend_test_client import authenticated_client

from backend import database


def make_job(name="Job service"):
    with authenticated_client(app) as client:
        organisation = client.post("/api/organisations", json={"name": name}).json()
    organisation_id = uuid.UUID(organisation["id"])
    with database.SessionLocal() as session:
        job, created = jobs.create_test_job(session, organisation_id, None)
    assert created is True
    return organisation_id, job.id


def test_job_attempts_retry_and_record_terminal_failure():
    organisation_id, job_id = make_job()
    with database.SessionLocal() as session:
        jobs.mark_queued(session, organisation_id, job_id)
        for expected_attempt in (1, 2):
            assert (
                jobs.start_attempt(session, organisation_id, job_id) == expected_attempt
            )
            assert (
                jobs.record_failure(
                    session, organisation_id, job_id, "temporary failure"
                )
                is True
            )
        assert jobs.start_attempt(session, organisation_id, job_id) == 3
        assert (
            jobs.record_failure(session, organisation_id, job_id, "permanent failure")
            is False
        )
        failed = jobs.get_job(session, organisation_id, job_id)
        assert failed.status == "failed"
        assert failed.error == "permanent failure"
        assert failed.completed_at is not None
        assert jobs.start_attempt(session, organisation_id, job_id) is None
        jobs.mark_completed(session, organisation_id, job_id)


def test_job_can_complete_once_and_enqueue_errors_can_be_retried():
    organisation_id, job_id = make_job("Successful job")
    with database.SessionLocal() as session:
        jobs.mark_queued(session, organisation_id, job_id)
        assert jobs.start_attempt(session, organisation_id, job_id) == 1
        jobs.mark_completed(session, organisation_id, job_id)
        completed = jobs.get_job(session, organisation_id, job_id)
        assert completed.status == "completed"
        assert completed.completed_at is not None
        assert completed.error is None
        assert jobs.start_attempt(session, organisation_id, job_id) is None

    organisation_id, job_id = make_job("Queue retry")
    with database.SessionLocal() as session:
        jobs.record_enqueue_failure(session, organisation_id, job_id, "x" * 5000)
        pending = jobs.get_job(session, organisation_id, job_id)
        assert pending.status == "pending"
        assert len(pending.error) == 4096
        jobs.mark_queued(session, organisation_id, job_id)
        queued = jobs.get_job(session, organisation_id, job_id)
        assert queued.status == "queued"
        assert queued.error is None


def test_missing_jobs_and_organisations_are_ignored():
    missing_id = uuid.uuid4()
    with database.SessionLocal() as session:
        job, created = jobs.create_test_job(session, missing_id, "missing")
        assert job is None
        assert created is False
        assert jobs.get_job(session, missing_id, uuid.uuid4()) is None
        assert jobs.start_attempt(session, missing_id, uuid.uuid4()) is None
        assert (
            jobs.record_failure(session, missing_id, uuid.uuid4(), "missing") is False
        )
        jobs.mark_queued(session, missing_id, uuid.uuid4())
        jobs.record_enqueue_failure(session, missing_id, uuid.uuid4(), "missing")
        jobs.mark_completed(session, missing_id, uuid.uuid4())
