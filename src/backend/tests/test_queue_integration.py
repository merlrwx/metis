import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from backend.main import app
from backend.models import DocumentVersion
from backend.queue import broker
from fastapi.testclient import TestClient
from redis import Redis
from redis.exceptions import ResponseError

from backend import database

pytestmark = [
    pytest.mark.external_worker,
    pytest.mark.skipif(
        not os.environ.get("METIS_QUEUE_TESTS"),
        reason="requires the isolated Redis and PostgreSQL integration services",
    ),
]


def start_worker(**extra_env):
    environment = {
        **os.environ,
        "METIS_TEST_JOB_DELAY_SECONDS": "0.05",
        "METIS_TEST_JOB_FAIL_ATTEMPTS": "0",
        **extra_env,
    }
    worker_command = Path(sys.executable).with_name("metis-worker")
    return subprocess.Popen(
        [str(worker_command)],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_worker(worker, force=False):
    if worker.poll() is None:
        os.killpg(worker.pid, signal.SIGKILL if force else signal.SIGTERM)
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)


def job_status(client, organisation_id, job_id):
    return client.get(f"/api/organisations/{organisation_id}/jobs/{job_id}").json()


def wait_for_status(client, organisation_id, job_id, expected, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = job_status(client, organisation_id, job_id)
        if current["status"] == expected:
            return current
        time.sleep(0.05)
    raise AssertionError(
        f"Job did not reach {expected}: {job_status(client, organisation_id, job_id)}"
    )


def submit_job(client, organisation_id, key):
    response = client.post(
        "/api/jobs/test",
        json={"organisation_id": organisation_id},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()


def reset_stream(redis):
    try:
        redis.xgroup_destroy(broker.queue_name, broker.consumer_group_name)
    except ResponseError:
        pass
    redis.delete(broker.queue_name)


def test_worker_restart_retry_and_idempotency():
    redis = Redis.from_url(os.environ["REDIS_URL"])
    stream = broker.queue_name
    try:
        try:
            redis.xgroup_destroy(stream, broker.consumer_group_name)
        except ResponseError:
            pass
        redis.delete(stream)

        worker = start_worker(METIS_TEST_JOB_DELAY_SECONDS="30")
        with TestClient(app) as client:
            organisation = client.post(
                "/api/organisations", json={"name": "Worker recovery"}
            ).json()
            job = submit_job(client, organisation["id"], "restart-once")
            wait_for_status(client, organisation["id"], job["id"], "processing")
            stop_worker(worker, force=True)

            worker = start_worker()
            recovered = wait_for_status(
                client, organisation["id"], job["id"], "completed"
            )
            assert recovered["attempts"] == 2
            assert recovered["error"] is None

            duplicate = submit_job(client, organisation["id"], "restart-once")
            assert duplicate["id"] == job["id"]
            assert job_status(client, organisation["id"], job["id"])["attempts"] == 2

            stop_worker(worker)
            worker = start_worker(METIS_TEST_JOB_FAIL_ATTEMPTS="1")
            retry_job = submit_job(client, organisation["id"], "retry-once")
            completed_after_retry = wait_for_status(
                client, organisation["id"], retry_job["id"], "completed"
            )
            assert completed_after_retry["attempts"] == 2

            stop_worker(worker)
            worker = start_worker(METIS_TEST_JOB_FAIL_ATTEMPTS="10")
            failed_job = submit_job(client, organisation["id"], "always-fails")
            failed = wait_for_status(
                client, organisation["id"], failed_job["id"], "failed"
            )
            assert failed["attempts"] == 3
            assert failed["error"] == "simulated transient worker failure"
    finally:
        if "worker" in locals():
            stop_worker(worker)
        redis.close()


def test_uploaded_pdf_is_extracted_and_reuses_its_indexed_version(sample_pdf):
    redis = Redis.from_url(os.environ["REDIS_URL"])
    reset_stream(redis)
    worker = start_worker()
    try:
        with TestClient(app) as client:
            organisation = client.post(
                "/api/organisations", json={"name": "PDF ingestion"}
            ).json()
            path = f"/api/organisations/{organisation['id']}/documents/upload"
            response = client.post(
                path,
                files={
                    "file": (
                        "policy.pdf",
                        sample_pdf,
                        "application/pdf",
                    )
                },
            )
            assert response.status_code == 202, response.text
            upload = response.json()
            job = wait_for_status(
                client,
                organisation["id"],
                upload["job"]["id"],
                "indexed",
            )
            assert job["attempts"] == 1

            document = client.get(
                f"/api/organisations/{organisation['id']}/documents/"
                f"{upload['document']['id']}"
            ).json()
            assert document["ingestion_status"] == "indexed"

            search = client.post(
                f"/api/organisations/{organisation['id']}/search",
                json={
                    "query": "What do we do after a medication incident?",
                    "source_id": upload["document"]["source_id"],
                    "document_id": upload["document"]["id"],
                    "limit": 1,
                },
            )
            assert search.status_code == 200, search.text
            assert (
                search.json()["results"][0]["document_id"] == upload["document"]["id"]
            )
            assert (
                "Medication incident policy" in search.json()["results"][0]["content"]
            )

            other_organisation = client.post(
                "/api/organisations", json={"name": "No incident policy access"}
            ).json()
            cross_tenant_search = client.post(
                f"/api/organisations/{other_organisation['id']}/search",
                json={"query": "Medication incident policy"},
            )
            assert cross_tenant_search.status_code == 200
            assert cross_tenant_search.json()["results"] == []

            duplicate = client.post(
                path,
                files={
                    "file": (
                        "policy.pdf",
                        sample_pdf,
                        "application/pdf",
                    )
                },
            )
            assert duplicate.status_code == 202, duplicate.text
            assert duplicate.json()["job"]["id"] == job["id"]
            with database.SessionLocal() as session:
                version = session.get(
                    DocumentVersion, upload["document"]["current_version_id"]
                )
                assert version.extracted_text == "Medication incident policy"
                assert version.extraction_metadata["pages"] == [
                    {"page": 1, "start": 0, "end": 26}
                ]
    finally:
        stop_worker(worker)
        redis.close()
