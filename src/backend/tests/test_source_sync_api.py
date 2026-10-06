from unittest.mock import AsyncMock
from uuid import UUID

from backend.main import app
from backend.models import Source
from backend_test_client import authenticated_client, register_and_login

from backend import database, main


def test_source_sync_requires_admin_and_owned_source_and_hides_credentials(monkeypatch):
    publisher = AsyncMock()
    monkeypatch.setattr(main, "QUEUE_CONFIGURED", True)
    monkeypatch.setattr(main.synchronize_source, "kiq", publisher)
    with authenticated_client(app) as client:
        org = client.post(
            "/api/organisations", json={"name": "Connector tenant"}
        ).json()
        path = f"/api/organisations/{org['id']}/sources"
        denied = client.post(
            path,
            json={
                "name": "Unsafe",
                "type": "microsoft365",
                "configuration": {"client_secret": "must-not-be-stored"},
            },
        )
        assert denied.status_code == 422
        source = client.post(
            path,
            json={
                "name": "Library",
                "type": "microsoft365",
                "configuration": {"sync_enabled": True},
            },
        ).json()
        assert "sync_checkpoint" not in source
        assert (
            client.post(
                f"/api/organisations/{org['id']}/documents/upload",
                data={"source_id": source["id"]},
                files={"file": ("policy.txt", b"Policy", "text/plain")},
            ).status_code
            == 404
        )
        response = client.post(f"{path}/{source['id']}/sync")
        assert (
            response.status_code == 202 and response.json()["sync_status"] == "queued"
        )
        publisher.assert_awaited_once_with(source["id"], org["id"])
        owner_headers = dict(client.headers)
        register_and_login(client, "connector-other@example.test")
        other_headers = dict(client.headers)
        client.headers.update(owner_headers)
        foreign = client.post(f"{path}/{source['id']}/sync", headers=other_headers)
        assert foreign.status_code == 404
        client.headers.update(owner_headers)
        member = client.post(
            f"/api/organisations/{org['id']}/members",
            json={"email": "connector-other@example.test", "role": "member"},
        )
        assert member.status_code == 201
        assert (
            client.post(
                f"{path}/{source['id']}/sync", headers=other_headers
            ).status_code
            == 403
        )
        publisher.side_effect = RuntimeError("offline")
        assert client.post(f"{path}/{source['id']}/sync").status_code == 503
        with database.SessionLocal() as session:
            saved = session.get(Source, UUID(source["id"]))
            assert (
                saved.sync_status == "failed" and saved.sync_error == "QueueUnavailable"
            )
