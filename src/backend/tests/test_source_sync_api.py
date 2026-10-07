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
                "organisation_library_approved": True,
                "configuration": {"client_secret": "must-not-be-stored"},
            },
        )
        assert denied.status_code == 422
        source = client.post(
            path,
            json={
                "name": "Library",
                "type": "microsoft365",
                "organisation_library_approved": True,
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


def test_connection_check_is_owned_and_redacts_failures(monkeypatch):
    from backend.connectors import microsoft365

    checks = []

    class Connector:
        def __init__(self, source_id):
            checks.append(str(source_id))

        async def check_connection(self):
            return {"library_name": "Approved policies"}

    monkeypatch.setattr(microsoft365, "Microsoft365Source", Connector)
    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Connected"}).json()["id"]
        other = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
        source = client.post(
            f"/api/organisations/{org}/sources",
            json={
                "name": "Policies",
                "type": "microsoft365",
                "organisation_library_approved": True,
            },
        ).json()["id"]
        path = f"/api/organisations/{org}/sources/{source}/check"
        assert client.post(path.replace(org, other)).status_code == 404
        assert checks == []
        result = client.post(path)
        assert result.status_code == 200
        assert result.json()["connected"] is True
        assert result.json()["library_name"] == "Approved policies"

        async def fail(self):
            raise OSError("secret-token signed-url private-document")

        monkeypatch.setattr(Connector, "check_connection", fail)
        result = client.post(path)
        assert result.json()["connected"] is False
        assert "secret-token" not in result.text
        listed = client.get(f"/api/organisations/{org}/sources")
        assert "secret-token" not in listed.text
        assert listed.json()[0]["configuration"]["connection_error"] == "OSError"


def test_pause_disconnect_and_group_scope_are_predictable(monkeypatch):
    monkeypatch.setattr(main, "QUEUE_CONFIGURED", True)
    publisher = AsyncMock()
    monkeypatch.setattr(main.synchronize_source, "kiq", publisher)
    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Controls"}).json()["id"]
        other = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
        source = client.post(
            f"/api/organisations/{org}/sources",
            json={
                "name": "Policies",
                "type": "microsoft365",
                "organisation_library_approved": True,
            },
        ).json()["id"]
        document = client.post(
            f"/api/organisations/{org}/documents",
            json={"title": "Policy", "source_id": source},
        ).json()["id"]
        group = client.post(
            f"/api/organisations/{org}/groups",
            json={"name": "Policies only", "source_ids": [source]},
        ).json()["id"]
        path = f"/api/organisations/{org}/sources/{source}"
        assert (
            client.patch(path.replace(org, other), json={"paused": True}).status_code
            == 404
        )
        assert client.delete(path.replace(org, other)).status_code == 404
        paused = client.patch(path, json={"paused": True, "sync_enabled": True})
        assert paused.status_code == 200
        assert paused.json()["sync_status"] == "paused"
        assert client.post(path + "/sync").status_code == 409
        publisher.assert_not_awaited()
        assert (
            client.get(f"/api/organisations/{org}/documents/{document}").json()[
                "ingestion_status"
            ]
            != "deleted"
        )
        assert (
            client.patch(path, json={"paused": False}).json()["sync_status"] == "idle"
        )
        assert client.delete(path).status_code == 200
        assert client.get(f"/api/organisations/{org}/sources").json() == []
        assert (
            next(
                item
                for item in client.get(f"/api/organisations/{org}/groups").json()
                if item["id"] == group
            )["source_ids"]
            == []
        )
        assert (
            client.get(f"/api/organisations/{org}/documents/{document}").json()[
                "ingestion_status"
            ]
            == "deleted"
        )
        assert client.post(path + "/sync").status_code == 404
        assert client.post(path + "/check").status_code == 404
        assert (
            client.post(
                f"/api/organisations/{org}/search",
                json={"query": "Policy", "source_ids": [source]},
            ).status_code
            == 404
        )


def test_library_requires_organisation_wide_approval():
    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Approval"}).json()["id"]
        denied = client.post(
            f"/api/organisations/{org}/sources",
            json={"name": "Restricted", "type": "microsoft365"},
        )
        assert denied.status_code == 422
        assert "all organisation members" in denied.json()["detail"]
        assert client.get(f"/api/organisations/{org}/sources").json() == []


def test_busy_source_controls_wait_and_paused_tasks_skip_remote_access(monkeypatch):
    import asyncio
    from datetime import UTC, datetime

    from backend.services import source_sync

    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Busy"}).json()["id"]
        source_id = client.post(
            f"/api/organisations/{org}/sources",
            json={
                "name": "Library",
                "type": "microsoft365",
                "organisation_library_approved": True,
            },
        ).json()["id"]
        path = f"/api/organisations/{org}/sources/{source_id}"
        with database.SessionLocal() as session:
            source = session.get(Source, UUID(source_id))
            source.sync_started_at = datetime.now(UTC)
            source.sync_status = "syncing"
            session.commit()
        assert client.patch(path, json={"paused": True}).status_code == 409
        assert client.delete(path).status_code == 409
        with database.SessionLocal() as session:
            source = session.get(Source, UUID(source_id))
            source.sync_started_at = None
            session.commit()
        assert client.patch(path, json={"paused": True}).status_code == 200

        def forbidden(_):
            raise AssertionError("Paused tasks must not construct a connector")

        monkeypatch.setattr(source_sync, "Microsoft365Source", forbidden)
        result = asyncio.run(
            source_sync.sync_source(UUID(org), UUID(source_id), AsyncMock())
        )
        assert result["skipped"] is True
        assert client.delete(path).status_code == 200
        result = asyncio.run(
            source_sync.sync_source(UUID(org), UUID(source_id), AsyncMock())
        )
        assert result["skipped"] is True


def test_saved_citations_refresh_current_source_freshness_without_foreign_hydration():
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from backend.schemas import CitationView
    from backend.services.conversations import refresh_citation_sources

    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Freshness"}).json()["id"]
        other = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
        source_id = client.post(
            f"/api/organisations/{org}/sources",
            json={
                "name": "Library",
                "type": "microsoft365",
                "organisation_library_approved": True,
            },
        ).json()["id"]
        with database.SessionLocal() as session:
            source = session.get(Source, UUID(source_id))
            source.sync_status = "failed"
            source.last_synced_at = datetime.now(UTC)
            session.commit()
            citation = CitationView(
                chunk_id=uuid4(),
                document_id=uuid4(),
                document_title="Policy",
                source_id=UUID(source_id),
                source_name="Library",
                page=None,
                section=None,
                snippet="Policy",
            )
            refreshed = refresh_citation_sources(session, UUID(org), [citation])[0]
            assert refreshed.source_sync_status == "failed"
            assert refreshed.source_last_synced_at is not None
            assert refreshed.source_type == "microsoft365"
            assert citation.source_sync_status is None
            source.sync_status = "idle"
            source.configuration = {**source.configuration, "sync_enabled": True}
            assert (
                refresh_citation_sources(session, UUID(org), [citation])[
                    0
                ].source_freshness
                == "recent_snapshot"
            )
            source.last_synced_at = datetime.now(UTC) - timedelta(minutes=31)
            assert (
                refresh_citation_sources(session, UUID(org), [citation])[
                    0
                ].source_freshness
                == "stale"
            )
            foreign = refresh_citation_sources(session, UUID(other), [citation])[0]
            assert foreign.source_sync_status is None
