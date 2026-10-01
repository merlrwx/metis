import os
import uuid

import pytest
from backend.main import app
from backend.models import Document
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from backend import database


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="requires PostgreSQL"
)
def test_postgres_rejects_a_source_from_another_organisation():
    with TestClient(app) as client:
        first = client.post("/api/organisations", json={"name": "First"}).json()
        second = client.post("/api/organisations", json={"name": "Second"}).json()
        source = client.post(
            f"/api/organisations/{first['id']}/sources", json={"name": "First files"}
        ).json()

    with database.SessionLocal() as session:
        session.add(
            Document(
                organisation_id=uuid.UUID(second["id"]),
                source_id=uuid.UUID(source["id"]),
                title="Cross-tenant document",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
