import json
import re
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

SECRET = "frontend-tests-cookie-secret-with-32-chars"
ORG = "00000000-0000-0000-0000-0000000000aa"
USER = "00000000-0000-0000-0000-000000000001"


def me(role="owner", memberships=None):
    return {
        "id": USER,
        "email": "owner@example.test",
        "name": "Olive Owner",
        "created_at": "2026-11-14T00:00:00Z",
        "memberships": memberships
        if memberships is not None
        else [{"organisation_id": ORG, "organisation_name": "Acme", "role": role}],
    }


def document(**overrides):
    return {
        "id": str(uuid4()),
        "organisation_id": ORG,
        "source_id": None,
        "title": "policy.pdf",
        "source_uri": None,
        "current_version_id": str(uuid4()),
        "created_at": "2026-11-14T00:00:00Z",
        "updated_at": "2026-11-14T00:00:00Z",
        "ingestion_status": "indexed",
        "ingestion_error": None,
        **overrides,
    }


class FakeApi:
    """A scriptable stand-in for the Metis API that records every call."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.role = "owner"
        self.routes: dict[tuple[str, str], object] = {}
        self.access = "access-1"

    def on(self, method: str, pattern: str, response):
        self.routes[(method, pattern)] = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = None
        if request.content and request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            body = json.loads(request.content)
        self.calls.append((request.method, path, body))
        for (method, pattern), response in self.routes.items():
            if method == request.method and re.fullmatch(pattern, path):
                result = response(request) if callable(response) else response
                if isinstance(result, httpx.Response):
                    return result
                status, payload = result
                return httpx.Response(status, json=payload)
        if path == "/api/auth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": self.access,
                    "refresh_token": "refresh-1",
                    "token_type": "bearer",
                    "expires_in": 1800,
                },
            )
        if path in ("/api/auth/logout",):
            return httpx.Response(204)
        if path == "/api/auth/me" and request.method == "GET":
            return httpx.Response(200, json=me(self.role))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(204)

    def called(self, method: str, path: str) -> list:
        return [body for m, p, body in self.calls if m == method and p == path]


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def app(api, monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "http://backend.test")
    monkeypatch.setenv("METIS_SESSION_COOKIE_SECRET", SECRET)
    monkeypatch.delenv("METIS_PUBLIC_URL", raising=False)
    from metis_web.app import create_app

    return create_app(transport=httpx.MockTransport(api))


@pytest.fixture
def anon(app):
    with TestClient(app, base_url="http://testserver") as client:
        yield client


def csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, "page has no csrf token"
    return match.group(1)


@pytest.fixture
def client(anon):
    page = anon.get("/login")
    response = anon.post(
        "/login",
        data={
            "email": "owner@example.test",
            "password": "pw",
            "csrf_token": csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    anon.token = csrf(page)
    return anon


def post(client, path, data=None, **kwargs):
    return client.post(
        path,
        data={**(data or {}), "csrf_token": client.token},
        follow_redirects=kwargs.pop("follow_redirects", False),
        **kwargs,
    )
