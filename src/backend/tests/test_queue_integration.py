import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from backend.chat import ChatCompletion
from backend.main import app
from backend.models import DocumentVersion
from backend.queue import broker
from backend_test_client import (
    TEST_PASSWORD,
    authenticated_client,
    login_as,
    register_and_login,
)
from redis import Redis
from redis.exceptions import ResponseError

from backend import chat, database

pytestmark = [
    pytest.mark.external_worker,
    pytest.mark.skipif(
        not os.environ.get("METIS_QUEUE_TESTS"),
        reason="requires the isolated Redis and PostgreSQL integration services",
    ),
]


class FakeChatProvider:
    model_id = "test-chat-v1"

    def __init__(self):
        self.calls = []

    def generate(self, messages):
        self.calls.append(messages)
        return ChatCompletion(
            "Notify the supervisor and record the incident [C1].",
            self.model_id,
            21,
            8,
        )


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
        with authenticated_client(app) as client:
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


def test_uploaded_pdf_is_extracted_and_reuses_its_indexed_version(
    sample_pdf, monkeypatch
):
    redis = Redis.from_url(os.environ["REDIS_URL"])
    reset_stream(redis)
    worker = start_worker()
    fake_chat = FakeChatProvider()
    monkeypatch.setattr(chat, "get_chat_provider", lambda: fake_chat)
    try:
        with authenticated_client(app) as client:
            organisation = client.post(
                "/api/organisations", json={"name": "PDF ingestion"}
            ).json()
            path = f"/api/organisations/{organisation['id']}/documents/upload"
            response = client.post(
                path,
                files={
                    "file": (
                        "secret-policy.pdf",
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

            chat_path = f"/api/organisations/{organisation['id']}/chat"
            first_turn = client.post(
                chat_path,
                json={"message": "What do we do after a medication incident?"},
            )
            assert first_turn.status_code == 200, first_turn.text
            first_answer = first_turn.json()
            assert "notify the supervisor" in first_answer["answer"].casefold()
            assert first_answer["model_id"] == "test-chat-v1"
            assert first_answer["usage"] == {"input_tokens": 21, "output_tokens": 8}
            assert (
                first_answer["citations"][0]["document_id"] == upload["document"]["id"]
            )

            second_turn = client.post(
                chat_path,
                json={
                    "conversation_id": first_answer["conversation_id"],
                    "message": "Repeat the medication incident policy.",
                },
            )
            assert second_turn.status_code == 200, second_turn.text
            assert [message[0] for message in fake_chat.calls[1][-3:]] == [
                "user",
                "assistant",
                "human",
            ]

            conversation = client.get(
                f"/api/organisations/{organisation['id']}/conversations/"
                f"{first_answer['conversation_id']}"
            )
            assert conversation.status_code == 200, conversation.text
            assert [message["role"] for message in conversation.json()["messages"]] == [
                "user",
                "assistant",
                "user",
                "assistant",
            ]
            assert (
                conversation.json()["messages"][1]["citations"][0]["document_title"]
                == "secret-policy.pdf"
            )

            owner_email = client.get("/api/auth/me").json()["email"]
            register_and_login(client, "tenant-b@example.test")
            other_organisation = client.post(
                "/api/organisations", json={"name": "Tenant B"}
            ).json()
            cross_tenant_search = client.post(
                f"/api/organisations/{other_organisation['id']}/search",
                json={"query": "Medication incident policy"},
            )
            assert cross_tenant_search.status_code == 200
            assert cross_tenant_search.json()["results"] == []
            hidden_document = client.get(
                f"/api/organisations/{organisation['id']}/documents/"
                f"{upload['document']['id']}"
            )
            assert hidden_document.status_code == 404
            empty_chat = client.post(
                f"/api/organisations/{other_organisation['id']}/chat",
                json={"message": "What do we do after a medication incident?"},
            )
            assert empty_chat.status_code == 200, empty_chat.text
            assert empty_chat.json()["citations"] == []
            assert "couldn't find enough" in empty_chat.json()["answer"]
            assert len(fake_chat.calls) == 2
            cross_tenant_conversation = client.get(
                f"/api/organisations/{other_organisation['id']}/conversations/"
                f"{first_answer['conversation_id']}"
            )
            assert cross_tenant_conversation.status_code == 404

            login_as(client, owner_email, TEST_PASSWORD)
            duplicate = client.post(
                path,
                files={
                    "file": (
                        "secret-policy.pdf",
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


def test_pending_job_reconciliation_delivers_to_worker_and_exports_metrics():
    from datetime import UTC, datetime, timedelta
    from urllib.request import urlopen
    from uuid import UUID

    from backend.models import IngestionJob

    redis = Redis.from_url(os.environ["REDIS_URL"])
    reset_stream(redis)
    worker = start_worker(METIS_WORKER_METRICS_PORT="19101")
    try:
        with authenticated_client(app) as client:
            organisation = client.post(
                "/api/organisations", json={"name": "Reconciliation"}
            ).json()
            with database.SessionLocal() as session:
                job = IngestionJob(
                    organisation_id=UUID(organisation["id"]),
                    status="pending",
                    created_at=datetime.now(UTC) - timedelta(minutes=10),
                )
                session.add(job)
                session.commit()
                job_id = str(job.id)
            reconcile = Path(sys.executable).with_name("metis-reconcile")
            subprocess.run(
                [str(reconcile)], check=True, capture_output=True, timeout=20
            )
            completed = wait_for_status(client, organisation["id"], job_id, "completed")
            assert completed["attempts"] == 1
            subprocess.run(
                [str(reconcile)], check=True, capture_output=True, timeout=20
            )
            assert job_status(client, organisation["id"], job_id)["attempts"] == 1
            with urlopen("http://127.0.0.1:19101/metrics", timeout=5) as response:
                metrics = response.read().decode()
            assert "metis_jobs_completed_total 1.0" in metrics
            assert "metis_job_duration_seconds_count 1.0" in metrics
    finally:
        stop_worker(worker)
        reset_stream(redis)


def test_external_source_changes_index_and_deletions_remove_retrieval(monkeypatch):
    from dataclasses import replace
    from datetime import UTC, datetime
    from uuid import UUID

    from backend.connectors.base import DocumentChanges, RemoteDocument
    from backend.models import Source
    from backend.services.source_sync import synchronize
    from backend.storage import get_object_storage
    from backend.tasks import process_ingestion_job

    redis = Redis.from_url(os.environ["REDIS_URL"])
    reset_stream(redis)
    worker = start_worker()
    remote = RemoteDocument(
        "external-file",
        "policy.txt",
        "text/plain",
        "v1",
        datetime.now(UTC),
        "https://example.sharepoint.com/policy.txt",
    )

    class FixtureLibrary:
        deleted = False
        changed = False

        async def list_documents(self, checkpoint=None):
            return DocumentChanges(
                [
                    replace(
                        remote,
                        deleted=self.deleted,
                        etag="v2" if self.changed else "v1",
                    )
                ],
                "next-checkpoint",
            )

        async def fetch_document(self, document):
            return (
                b"Medication incident: notify the supervisor and record the incident."
                + (
                    b" Changed policy requires a register entry."
                    if self.changed
                    else b""
                )
            )

    library = FixtureLibrary()
    published = []

    async def publisher(job_id, organisation_id):
        published.append((job_id, organisation_id))
        await process_ingestion_job.kiq(job_id, organisation_id)

    try:
        with authenticated_client(app) as client:
            organisation = client.post(
                "/api/organisations", json={"name": "Connected library"}
            ).json()
            foreign = client.post(
                "/api/organisations", json={"name": "Other tenant"}
            ).json()
            source = client.post(
                f"/api/organisations/{organisation['id']}/sources",
                json={
                    "name": "Library",
                    "type": "microsoft365",
                    "organisation_library_approved": True,
                },
            ).json()
            with database.SessionLocal() as session:
                client.portal.call(
                    synchronize,
                    session,
                    UUID(organisation["id"]),
                    UUID(source["id"]),
                    library,
                    get_object_storage(),
                    publisher,
                )
            completed = wait_for_status(
                client, organisation["id"], published[-1][0], "indexed"
            )
            assert completed["attempts"] == 1
            query = {"query": "medication incident supervisor"}
            results = client.post(
                f"/api/organisations/{organisation['id']}/search", json=query
            ).json()["results"]
            assert len(results) == 1
            document_id = results[0]["document_id"]
            document = client.get(
                f"/api/organisations/{organisation['id']}/documents/{document_id}"
            ).json()
            assert document["source_uri"] == remote.source_url
            assert (
                client.post(
                    f"/api/organisations/{foreign['id']}/search", json=query
                ).json()["results"]
                == []
            )
            monkeypatch.setattr(chat, "get_chat_provider", FakeChatProvider)
            answer = client.post(
                f"/api/organisations/{organisation['id']}/chat",
                json={"message": "What happens after a medication incident?"},
            ).json()
            assert answer["citations"][0]["source_url"] == remote.source_url
            library.changed = True
            with database.SessionLocal() as session:
                client.portal.call(
                    synchronize,
                    session,
                    UUID(organisation["id"]),
                    UUID(source["id"]),
                    library,
                    get_object_storage(),
                    publisher,
                )
            wait_for_status(client, organisation["id"], published[-1][0], "indexed")
            results = client.post(
                f"/api/organisations/{organisation['id']}/search", json=query
            ).json()["results"]
            assert len(results) == 1 and "register entry" in results[0]["content"]
            library.deleted = True
            with database.SessionLocal() as session:
                client.portal.call(
                    synchronize,
                    session,
                    UUID(organisation["id"]),
                    UUID(source["id"]),
                    library,
                    get_object_storage(),
                    publisher,
                )
                saved = session.get(Source, UUID(source["id"]))
                assert saved.sync_status == "idle"
            assert (
                client.post(
                    f"/api/organisations/{organisation['id']}/search", json=query
                ).json()["results"]
                == []
            )
            assert (
                client.get(
                    f"/api/organisations/{organisation['id']}/documents/{document_id}"
                ).json()["ingestion_status"]
                == "deleted"
            )
    finally:
        stop_worker(worker)
        reset_stream(redis)


def test_selected_payslip_context_bypasses_lexical_cutoff_and_rejects_foreign_document(
    monkeypatch,
):
    from backend.services.rag import NO_EVIDENCE_ANSWER

    class PayslipProvider:
        model_id = "test-payslip"

        def generate(self, messages):
            assert "Net payment 2000.00" in messages[0][1]
            return ChatCompletion(
                "Your net payment was 2000.00 for the fortnight [C1].",
                self.model_id,
                20,
                10,
            )

    redis = Redis.from_url(os.environ["REDIS_URL"])
    reset_stream(redis)
    worker = start_worker()
    monkeypatch.setattr(chat, "get_chat_provider", PayslipProvider)
    try:
        with authenticated_client(app) as client:
            org = client.post(
                "/api/organisations", json={"name": "Synthetic payslip"}
            ).json()["id"]
            other_org = client.post(
                "/api/organisations", json={"name": "Other tenant"}
            ).json()["id"]
            uploaded = client.post(
                f"/api/organisations/{org}/documents/upload",
                files={
                    "file": (
                        "payslip.txt",
                        b"PAYSLIP\nPeriod: fortnight\nGross earnings 2500.00\nTax withheld 500.00\nNet payment 2000.00",
                        "text/plain",
                    )
                },
            ).json()
            wait_for_status(client, org, uploaded["job"]["id"], "indexed")
            payload = {"message": "How much did I get paid in 2 weeks?"}
            unscoped = client.post(f"/api/organisations/{org}/chat", json=payload)
            assert unscoped.json()["answer"] == NO_EVIDENCE_ANSWER
            payload["document_id"] = uploaded["document"]["id"]
            answered = client.post(f"/api/organisations/{org}/chat", json=payload)
            assert answered.status_code == 200, answered.text
            assert (
                answered.json()["citations"][0]["document_id"] == payload["document_id"]
            )
            assert "2000.00" in answered.json()["answer"]
            denied = client.post(f"/api/organisations/{other_org}/chat", json=payload)
            assert denied.status_code == 404
    finally:
        stop_worker(worker)
        reset_stream(redis)


def test_reindex_resumes_maintenance_and_keeps_other_tenant_untouched():
    from uuid import UUID

    from backend.models import Chunk, Organisation
    from backend.reindex import rebuild
    from sqlalchemy import select

    redis = Redis.from_url(os.environ["REDIS_URL"])
    reset_stream(redis)
    worker = start_worker()
    try:
        with authenticated_client(app) as client:
            org = client.post("/api/organisations", json={"name": "Re-index A"}).json()[
                "id"
            ]
            other = client.post(
                "/api/organisations", json={"name": "Re-index B"}
            ).json()["id"]
            uploaded = client.post(
                f"/api/organisations/{org}/documents/upload",
                files={
                    "file": (
                        "policy.txt",
                        b"A synthetic medication incident policy.",
                        "text/plain",
                    )
                },
            ).json()
            wait_for_status(client, org, uploaded["job"]["id"], "indexed")
            unreadable = client.post(
                f"/api/organisations/{org}/documents/upload",
                files={"file": ("empty.txt", b"", "text/plain")},
            ).json()
            failed = wait_for_status(client, org, unreadable["job"]["id"], "failed")
            with database.SessionLocal() as session:
                first = session.get(Organisation, UUID(org))
                first.index_status = "reindexing"
                chunk = session.scalar(
                    select(Chunk).where(Chunk.organisation_id == UUID(org))
                )
                chunk.embedding_model = "old-model"
                session.commit()
            assert (
                client.post(
                    f"/api/organisations/{org}/chat",
                    json={"message": "medication incident"},
                ).status_code
                == 503
            )
            client.portal.call(rebuild, UUID(org), 15)
            with database.SessionLocal() as session:
                first = session.get(Organisation, UUID(org))
                assert (
                    first.index_status == "ready"
                    and first.index_model == "metis:feature-hash-v1"
                )
                assert session.get(Organisation, UUID(other)).index_model is None
                assert set(
                    session.scalars(
                        select(Chunk.embedding_model).where(
                            Chunk.organisation_id == UUID(org)
                        )
                    )
                ) == {"metis:feature-hash-v1"}
            assert (
                job_status(client, org, unreadable["job"]["id"])["attempts"]
                == failed["attempts"]
            )
            assert (
                job_status(client, org, unreadable["job"]["id"])["status"] == "failed"
            )
            # A completed rebuild is idempotent and does not reprocess unchanged rows.
            client.portal.call(rebuild, UUID(org), 15)
            assert client.post(
                f"/api/organisations/{org}/search",
                json={"query": "medication incident"},
            ).json()["results"]
    finally:
        stop_worker(worker)
        reset_stream(redis)
