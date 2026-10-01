from backend.main import app
from backend.models import Organisation
from backend_test_client import (
    TEST_PASSWORD,
    authenticated_client,
    login_as,
    register_and_login,
)
from fastapi.testclient import TestClient

from backend import database


def test_authentication_is_required_and_login_returns_no_password_data():
    with TestClient(app) as client:
        assert client.get("/api/auth/me").status_code == 401
        assert (
            client.post(
                "/api/organisations", json={"name": "Unauthenticated"}
            ).status_code
            == 401
        )

        user = register_and_login(client, "Owner@Example.test")
        assert user["email"] == "owner@example.test"
        assert user["memberships"] == []
        assert "password_hash" not in user
        assert (
            client.post(
                "/api/auth/register",
                json={
                    "email": "OWNER@example.test",
                    "name": "Duplicate",
                    "password": TEST_PASSWORD,
                },
            ).status_code
            == 409
        )

        bad_login = client.post(
            "/api/auth/token",
            data={"username": "owner@example.test", "password": "wrong-password"},
        )
        assert bad_login.status_code == 401

        client.headers["Authorization"] = "Bearer not-a-valid-token"
        assert client.get("/api/auth/me").status_code == 401


def test_membership_roles_conversation_ownership_and_audit_events(monkeypatch):
    monkeypatch.setattr(
        "backend.main.knowledge.search_chunks", lambda *_args, **_kwargs: []
    )
    with authenticated_client(app) as client:
        owner_email = client.get("/api/auth/me").json()["email"]
        organisation = client.post(
            "/api/organisations", json={"name": "Membership clinic"}
        ).json()
        organisation_id = organisation["id"]
        owner_id = client.get("/api/auth/me").json()["id"]

        assert (
            client.get(f"/api/organisations/{organisation_id}/members").status_code
            == 200
        )

        admin = register_and_login(client, "admin@example.test")
        login_as(client, owner_email)
        added_admin = client.post(
            f"/api/organisations/{organisation_id}/members",
            json={"email": admin["email"], "role": "admin"},
        )
        assert added_admin.status_code == 201
        assert added_admin.json()["role"] == "admin"

        member = register_and_login(client, "member@example.test")
        login_as(client, owner_email)
        added_member = client.post(
            f"/api/organisations/{organisation_id}/members",
            json={"email": member["email"], "role": "member"},
        )
        assert added_member.status_code == 201

        login_as(client, admin["email"])
        assert (
            client.get(f"/api/organisations/{organisation_id}/members").status_code
            == 200
        )
        assert (
            client.post(
                f"/api/organisations/{organisation_id}/members",
                json={"email": "another@example.test", "role": "admin"},
            ).status_code
            == 403
        )
        assert (
            client.get(f"/api/organisations/{organisation_id}/audit-events").status_code
            == 200
        )

        admin_chat = client.post(
            f"/api/organisations/{organisation_id}/chat",
            json={"message": "What is in the knowledge base?"},
        )
        assert admin_chat.status_code == 200
        conversation_id = admin_chat.json()["conversation_id"]

        login_as(client, member["email"])
        assert (
            client.get(f"/api/organisations/{organisation_id}/members").status_code
            == 403
        )
        assert (
            client.get(
                f"/api/organisations/{organisation_id}/conversations/{conversation_id}"
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/organisations/{organisation_id}/sources",
                json={"name": "Member cannot add sources"},
            ).status_code
            == 403
        )

        login_as(client, owner_email)
        transcript = client.get(
            f"/api/organisations/{organisation_id}/conversations/{conversation_id}"
        )
        assert transcript.status_code == 200
        events = client.get(f"/api/organisations/{organisation_id}/audit-events")
        assert events.status_code == 200
        actions = [event["action"] for event in events.json()]
        assert "organisation.created" in actions
        assert actions.count("member.added") == 2
        assert "chat.turn" in actions
        assert all("message" not in event["details"] for event in events.json())
        assert owner_id == client.get("/api/auth/me").json()["id"]


def test_unowned_legacy_organisation_requires_bootstrap_token():
    with database.SessionLocal() as session:
        organisation = Organisation(name="Legacy organisation")
        session.add(organisation)
        session.commit()
        session.refresh(organisation)
        organisation_id = organisation.id

    with authenticated_client(app) as client:
        denied = client.post(
            f"/api/organisations/{organisation_id}/claim",
            headers={"X-Metis-Bootstrap-Token": "wrong-token"},
        )
        assert denied.status_code == 403
        claimed = client.post(
            f"/api/organisations/{organisation_id}/claim",
            headers={"X-Metis-Bootstrap-Token": "metis-ci-only-bootstrap-token"},
        )
        assert claimed.status_code == 200
        assert claimed.json()["name"] == "Legacy organisation"
        assert client.get("/api/auth/me").json()["memberships"][0]["role"] == "owner"

        repeated_claim = client.post(
            f"/api/organisations/{organisation_id}/claim",
            headers={"X-Metis-Bootstrap-Token": "metis-ci-only-bootstrap-token"},
        )
        assert repeated_claim.status_code == 409
