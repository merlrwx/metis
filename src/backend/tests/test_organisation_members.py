from datetime import timedelta
from uuid import uuid4

import pytest
from backend.main import app
from backend.models import Invitation
from backend_test_client import TEST_PASSWORD
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import auth, database


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def account(client, email=None):
    email = email or f"{uuid4()}@example.test"
    client.post(
        "/api/auth/register",
        json={"email": email, "name": "Member", "password": TEST_PASSWORD},
    )
    tokens = client.post(
        "/api/auth/token", data={"username": email, "password": TEST_PASSWORD}
    ).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    user_id = client.get("/api/auth/me", headers=headers).json()["id"]
    return email, headers, user_id


def organisation(client, headers, name="Tenant"):
    return client.post(
        "/api/organisations", headers=headers, json={"name": name}
    ).json()["id"]


def add(client, org, headers, email, role="member"):
    response = client.post(
        f"/api/organisations/{org}/members",
        headers=headers,
        json={"email": email, "role": role},
    )
    assert response.status_code == 201, response.text


def test_second_owner_allows_original_owner_to_step_down(client):
    _, owner, owner_id = account(client)
    org = organisation(client, owner)
    other_email, other, other_id = account(client)
    add(client, org, owner, other_email)
    base = f"/api/organisations/{org}/members"
    assert (
        client.patch(
            f"{base}/{other_id}", headers=owner, json={"role": "owner"}
        ).json()["role"]
        == "owner"
    )
    assert (
        client.patch(
            f"{base}/{owner_id}", headers=owner, json={"role": "member"}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"{base}/{other_id}", headers=owner, json={"role": "member"}
        ).status_code
        == 403
    )
    assert client.delete(f"{base}/{owner_id}", headers=owner).status_code == 204
    assert client.get(base, headers=other).status_code == 200


def test_member_cannot_remove_others_and_unknown_members_are_404(client):
    _, owner, owner_id = account(client)
    org = organisation(client, owner)
    member_email, member, _ = account(client)
    add(client, org, owner, member_email)
    base = f"/api/organisations/{org}/members"
    assert client.delete(f"{base}/{owner_id}", headers=member).status_code == 403
    assert client.delete(f"{base}/{uuid4()}", headers=owner).status_code == 404
    assert (
        client.patch(
            f"{base}/{uuid4()}", headers=owner, json={"role": "member"}
        ).status_code
        == 404
    )


def test_admin_cannot_remove_another_admin(client):
    _, owner, _ = account(client)
    org = organisation(client, owner)
    first_email, first, _ = account(client)
    second_email, _, second_id = account(client)
    add(client, org, owner, first_email, "admin")
    add(client, org, owner, second_email, "admin")
    response = client.delete(
        f"/api/organisations/{org}/members/{second_id}", headers=first
    )
    assert response.status_code == 403


def test_reinviting_replaces_the_previous_link(client):
    _, owner, _ = account(client)
    org = organisation(client, owner)
    base = f"/api/organisations/{org}/invitations"
    first = client.post(base, headers=owner, json={"email": "new@example.test"}).json()
    second = client.post(base, headers=owner, json={"email": "NEW@example.test"}).json()
    assert [item["id"] for item in client.get(base, headers=owner).json()] == [
        second["id"]
    ]
    _, invitee, _ = account(client, "new@example.test")
    assert (
        client.post(
            "/api/invitations/accept", headers=invitee, json={"token": first["token"]}
        ).status_code
        == 404
    )
    preview = client.post("/api/invitations/preview", json={"token": second["token"]})
    assert preview.status_code == 200
    assert preview.json()["organisation_name"] == "Tenant"
    assert preview.headers["cache-control"] == "no-store"


def test_expired_invitations_cannot_be_accepted(client):
    _, owner, _ = account(client)
    org = organisation(client, owner)
    created = client.post(
        f"/api/organisations/{org}/invitations",
        headers=owner,
        json={"email": "late@example.test"},
    ).json()
    with database.SessionLocal() as session:
        session.scalar(select(Invitation)).expires_at = auth.utcnow() - timedelta(1)
        session.commit()
    _, late, _ = account(client, "late@example.test")
    response = client.post(
        "/api/invitations/accept", headers=late, json={"token": created["token"]}
    )
    assert response.status_code == 404
    assert (
        client.get(f"/api/organisations/{org}/invitations", headers=owner).json() == []
    )


def test_invitation_rules_for_admins_and_existing_members(client):
    _, owner, _ = account(client)
    org = organisation(client, owner)
    admin_email, admin, _ = account(client)
    add(client, org, owner, admin_email, "admin")
    base = f"/api/organisations/{org}/invitations"
    assert (
        client.post(
            base, headers=admin, json={"email": "x@example.test", "role": "admin"}
        ).status_code
        == 403
    )
    assert (
        client.post(base, headers=admin, json={"email": admin_email}).status_code == 409
    )
    assert client.delete(f"{base}/{uuid4()}", headers=owner).status_code == 404
    events = client.get(f"/api/organisations/{org}/audit-events", headers=owner).json()
    assert all("token" not in str(event["details"]) for event in events)


def test_accepting_when_already_a_member_keeps_the_existing_role(client):
    _, owner, _ = account(client)
    org = organisation(client, owner)
    created = client.post(
        f"/api/organisations/{org}/invitations",
        headers=owner,
        json={"email": "dual@example.test", "role": "member"},
    ).json()
    email, invitee, _ = account(client, "dual@example.test")
    add(client, org, owner, email, "admin")
    response = client.post(
        "/api/invitations/accept", headers=invitee, json={"token": created["token"]}
    )
    assert response.status_code == 200
    roles = client.get("/api/auth/me", headers=invitee).json()["memberships"]
    assert roles[0]["role"] == "admin"


def test_rename_validates_and_audits(client):
    _, owner, _ = account(client)
    org = organisation(client, owner)
    path = f"/api/organisations/{org}"
    assert client.patch(path, headers=owner, json={"name": "  "}).status_code == 422
    assert client.patch(path, headers=owner, json={"name": "New"}).status_code == 200
    actions = [
        event["action"]
        for event in client.get(f"{path}/audit-events", headers=owner).json()
    ]
    assert "organisation.renamed" in actions
