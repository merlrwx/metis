from datetime import timedelta
from uuid import uuid4

import jwt
import pytest
from backend.main import app
from backend.models import AuthAttempt, AuthSession
from backend_test_client import TEST_PASSWORD
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import auth, database


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def register(client, email=None, password=TEST_PASSWORD):
    email = email or f"{uuid4()}@example.test"
    response = client.post(
        "/api/auth/register",
        json={"email": email, "name": "Account", "password": password},
    )
    assert response.status_code == 201
    return email


def login(client, email, password=TEST_PASSWORD, user_agent="pytest-agent"):
    response = client.post(
        "/api/auth/token",
        data={"username": email, "password": password},
        headers={"User-Agent": user_agent},
    )
    assert response.status_code == 200, response.text
    return response.json()


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_login_creates_a_session_with_device_details(client):
    tokens = login(client, register(client))
    assert tokens["expires_in"] == 1800
    sessions = client.get("/api/auth/sessions", headers=bearer(tokens)).json()
    assert sessions[0]["current"] is True
    assert sessions[0]["user_agent"] == "pytest-agent"
    claims = jwt.decode(
        tokens["access_token"],
        options={"verify_signature": False},
        algorithms=["HS256"],
    )
    assert claims["typ"] == "access" and claims["sid"] == sessions[0]["id"]


def test_expired_session_rejects_access_and_refresh(client):
    tokens = login(client, register(client))
    with database.SessionLocal() as session:
        row = session.scalar(select(AuthSession))
        row.expires_at = auth.utcnow() - timedelta(seconds=1)
        session.commit()
    assert client.get("/api/auth/me", headers=bearer(tokens)).status_code == 401
    assert (
        client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("token", ["not-a-session.value", "missing-separator"])
def test_malformed_refresh_tokens_are_rejected(client, token):
    response = client.post("/api/auth/refresh", json={"refresh_token": token})
    assert response.status_code == 401


def test_refresh_tokens_cannot_be_used_as_access_tokens(client):
    tokens = login(client, register(client))
    headers = {"Authorization": f"Bearer {tokens['refresh_token']}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_non_access_token_type_is_rejected(client):
    tokens = login(client, register(client))
    claims = jwt.decode(
        tokens["access_token"],
        options={"verify_signature": False},
        algorithms=["HS256"],
    )
    claims["typ"] = "refresh"
    forged = jwt.encode(claims, auth._secret_key(), algorithm="HS256")
    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_login_failures_are_throttled_and_cleared_by_success(client, monkeypatch):
    monkeypatch.setenv("METIS_LOGIN_MAX_FAILURES", "3")
    email = register(client)
    for _ in range(2):
        assert (
            client.post(
                "/api/auth/token", data={"username": email, "password": "wrong"}
            ).status_code
            == 401
        )
    login(client, email.upper())
    with database.SessionLocal() as session:
        assert session.scalar(select(func.count(AuthAttempt.id))) == 0
    for _ in range(3):
        client.post("/api/auth/token", data={"username": email, "password": "wrong"})
    throttled = client.post(
        "/api/auth/token", data={"username": email, "password": TEST_PASSWORD}
    )
    assert throttled.status_code == 429
    assert 1 <= int(throttled.headers["Retry-After"]) <= 15 * 60 + 1


def test_unknown_emails_are_throttled_without_storing_the_address(client, monkeypatch):
    monkeypatch.setenv("METIS_LOGIN_MAX_FAILURES", "3")
    for _ in range(3):
        client.post(
            "/api/auth/token", data={"username": "ghost@example.test", "password": "x"}
        )
    with database.SessionLocal() as session:
        identifiers = session.scalars(select(AuthAttempt.identifier)).all()
    assert len(identifiers) == 3
    assert all("ghost" not in identifier for identifier in identifiers)
    assert (
        client.post(
            "/api/auth/token", data={"username": "ghost@example.test", "password": "x"}
        ).status_code
        == 429
    )


def test_old_failures_expire_from_the_window(client, monkeypatch):
    monkeypatch.setenv("METIS_LOGIN_MAX_FAILURES", "3")
    email = register(client)
    for _ in range(3):
        client.post("/api/auth/token", data={"username": email, "password": "wrong"})
    with database.SessionLocal() as session:
        for attempt in session.scalars(select(AuthAttempt)):
            attempt.created_at = auth.utcnow() - timedelta(minutes=16)
        session.commit()
    login(client, email)


def test_password_change_rejects_reuse(client):
    email = register(client)
    tokens = login(client, email)
    response = client.post(
        "/api/auth/password",
        headers=bearer(tokens),
        json={"current_password": TEST_PASSWORD, "new_password": TEST_PASSWORD},
    )
    assert response.status_code == 400


def test_revoked_session_cannot_be_revoked_again(client):
    email = register(client)
    first, second = login(client, email), login(client, email)
    sessions = client.get("/api/auth/sessions", headers=bearer(first)).json()
    other = next(item for item in sessions if not item["current"])["id"]
    assert (
        client.delete(f"/api/auth/sessions/{other}", headers=bearer(first)).status_code
        == 204
    )
    assert (
        client.delete(f"/api/auth/sessions/{other}", headers=bearer(first)).status_code
        == 404
    )
    assert client.get("/api/auth/me", headers=bearer(second)).status_code == 401


def test_profile_rejects_blank_names(client):
    tokens = login(client, register(client))
    response = client.patch("/api/auth/me", headers=bearer(tokens), json={"name": " "})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("name", "value"),
    [("METIS_ACCESS_TOKEN_MINUTES", "0"), ("METIS_SESSION_DAYS", "400")],
)
def test_lifetime_settings_are_bounded(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError):
        auth.access_token_lifetime() if "MINUTES" in name else auth.session_lifetime()


def test_missing_secret_returns_service_unavailable(client, monkeypatch):
    email = register(client)
    monkeypatch.setenv("METIS_AUTH_SECRET_KEY", "short")
    response = client.post(
        "/api/auth/token", data={"username": email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 503


def test_api_security_headers_and_no_store_for_auth(client):
    response = client.get("/api/info")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "no-store" not in response.headers.get("cache-control", "")
    refresh = client.post("/api/auth/refresh", json={"refresh_token": "x.y"})
    assert refresh.headers["cache-control"] == "no-store"


def test_refresh_rotation_reuse_logout_and_password_change(client):
    email = register(client)
    tokens = login(client, email)
    rotated = client.post(
        "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    ).json()
    assert client.get("/api/auth/me", headers=bearer(rotated)).status_code == 200
    assert (
        client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    assert client.get("/api/auth/me", headers=bearer(rotated)).status_code == 401

    current, other = login(client, email), login(client, email)
    assert (
        client.post(
            "/api/auth/sessions/revoke-others", headers=bearer(current)
        ).status_code
        == 204
    )
    assert client.get("/api/auth/me", headers=bearer(other)).status_code == 401
    assert (
        client.post(
            "/api/auth/password",
            headers=bearer(current),
            json={"current_password": "wrong", "new_password": "another-pass-123"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/auth/password",
            headers=bearer(current),
            json={
                "current_password": TEST_PASSWORD,
                "new_password": "another-pass-123",
            },
        ).status_code
        == 204
    )
    assert client.post("/api/auth/logout", headers=bearer(current)).status_code == 204
    assert client.get("/api/auth/me", headers=bearer(current)).status_code == 401
    login(client, email, "another-pass-123")


def test_profile_update_returns_current_user(client):
    tokens = login(client, register(client))
    response = client.patch(
        "/api/auth/me", headers=bearer(tokens), json={"name": " New Name "}
    )
    assert response.status_code == 200 and response.json()["name"] == "New Name"
