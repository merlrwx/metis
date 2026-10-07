from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from backend.models import AuditEvent
from backend.services import request_limits
from backend_test_client import new_authenticated_client
from fastapi import HTTPException
from sqlalchemy import func, select

from backend import database, main

# Admission commits independently; use real connections rather than nested savepoints.
pytestmark = pytest.mark.external_worker


def client_and_org(monkeypatch):
    monkeypatch.setattr(main.knowledge, "search_chunks", lambda *args, **kwargs: [])
    client = new_authenticated_client(main.app)
    org = client.post("/api/organisations", json={"name": "Budget tenant"}).json()["id"]
    return client, org


def test_cached_uuid_retry_does_not_consume_another_budget(monkeypatch):
    monkeypatch.setenv("METIS_USER_REQUESTS_PER_MINUTE", "1")
    client, org = client_and_org(monkeypatch)
    path = f"/api/organisations/{org}/chat"
    payload = {"message": "What is the policy?", "request_id": str(uuid4())}
    assert client.post(path, json=payload).status_code == 200
    assert client.post(path, json=payload).status_code == 200
    limited = client.post(path, json={"message": "Another question"})
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    other = client.post("/api/organisations", json={"name": "Other budget"}).json()[
        "id"
    ]
    assert (
        client.post(
            f"/api/organisations/{other}/chat", json={"message": "Independent question"}
        ).status_code
        == 200
    )


def test_failed_provider_attempt_still_consumes_budget(monkeypatch):
    monkeypatch.setenv("METIS_USER_REQUESTS_PER_MINUTE", "1")
    client, org = client_and_org(monkeypatch)

    def fail(*args, **kwargs):
        raise main.chat.ChatProviderError("unavailable")

    monkeypatch.setattr(main.rag, "answer_question", fail)
    path = f"/api/organisations/{org}/chat"
    assert client.post(path, json={"message": "Question"}).status_code == 503
    assert client.post(path, json={"message": "Retry"}).status_code == 429


def test_json_body_limit_covers_declared_and_chunked_bodies():
    client = new_authenticated_client(main.app)
    body = b'{"name":"' + b"x" * request_limits.JSON_BODY_LIMIT + b'"}'
    assert (
        client.post(
            "/api/auth/register",
            content=body,
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/api/auth/register",
            content=iter([body[:100], body[100:]]),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )


def test_organisation_budget_is_atomic_across_database_sessions(monkeypatch):
    if database.engine.dialect.name != "postgresql":
        pytest.skip("Row-lock concurrency requires PostgreSQL")
    monkeypatch.setenv("METIS_ORG_REQUESTS_PER_MINUTE", "1")
    client, org = client_and_org(monkeypatch)
    user = UUID(client.get("/api/auth/me").json()["id"])

    def admit():
        try:
            request_limits.enforce(UUID(org), user, "chat")
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: admit(), range(2)))
    assert sorted(results) == [200, 429]
    with database.SessionLocal() as session:
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.organisation_id == UUID(org),
                    AuditEvent.action == "request.accepted",
                )
            )
            == 1
        )
