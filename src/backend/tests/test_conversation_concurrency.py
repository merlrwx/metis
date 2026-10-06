import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from backend.main import app
from backend.models import ChatRequestRecord, Message
from backend_test_client import authenticated_client
from sqlalchemy import func, select

from backend import database

pytestmark = [
    pytest.mark.external_worker,
    pytest.mark.skipif(
        not os.environ.get("METIS_QUEUE_TESTS"),
        reason="requires isolated PostgreSQL outside a test transaction",
    ),
]


def test_concurrent_same_request_saves_one_turn(monkeypatch):
    monkeypatch.setattr(
        "backend.main.knowledge.search_chunks", lambda *args, **kwargs: []
    )
    with authenticated_client(app) as client:
        org = client.post(
            "/api/organisations", json={"name": "Concurrent retry"}
        ).json()["id"]
        payload = {"message": "What is the policy?", "request_id": str(uuid4())}

        def send():
            response = client.post(f"/api/organisations/{org}/chat", json=payload)
            assert response.status_code == 200
            return response.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: send(), range(2)))
        assert first == second
        with database.SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(Message)) == 2
            assert (
                session.scalar(select(func.count()).select_from(ChatRequestRecord)) == 1
            )
