from uuid import uuid4

import pytest
from backend.main import app
from backend.models import Organisation
from backend.services.knowledge import IndexUnavailable, ensure_index_ready
from backend_test_client import authenticated_client

from backend import database


def test_maintenance_and_model_mismatch_are_explicit_and_tenant_scoped():
    with database.SessionLocal() as session:
        first = Organisation(name="A", index_status="reindexing")
        second = Organisation(name="B", index_model="expected")
        session.add_all([first, second])
        session.commit()
        with pytest.raises(IndexUnavailable):
            ensure_index_ready(session, first.id, "expected")
        ensure_index_ready(session, second.id, "expected")
        with pytest.raises(IndexUnavailable):
            ensure_index_ready(session, second.id, "wrong")
        ensure_index_ready(session, uuid4(), "expected")


def test_chat_and_upload_report_maintenance_instead_of_missing_evidence():
    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Maintenance"}).json()[
            "id"
        ]
        with database.SessionLocal() as session:
            from uuid import UUID

            organisation = session.get(Organisation, UUID(org))
            organisation.index_status = "reindexing"
            session.commit()
        chat = client.post(
            f"/api/organisations/{org}/chat", json={"message": "A natural question"}
        )
        assert chat.status_code == 503
        upload = client.post(
            f"/api/organisations/{org}/documents/upload",
            files={"file": ("a.txt", b"Synthetic text", "text/plain")},
        )
        assert upload.status_code == 503


def test_invalid_embedding_configuration_is_a_service_error(monkeypatch):
    def invalid_provider():
        raise ValueError("Invalid embedding dimensions")

    monkeypatch.setattr(
        "backend.main.embeddings.get_embedding_provider", invalid_provider
    )
    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Configuration"}).json()[
            "id"
        ]
        for route, payload in [
            ("search", {"query": "synthetic"}),
            ("chat", {"message": "synthetic"}),
        ]:
            response = client.post(f"/api/organisations/{org}/{route}", json=payload)
            assert response.status_code == 503
            assert response.json()["detail"] == "Embedding provider is unavailable"
