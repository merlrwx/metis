from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient

TEST_PASSWORD = "metis-test-password"


def register_and_login(
    client: TestClient, email: str | None = None, password: str = TEST_PASSWORD
) -> dict:
    email = email or f"{uuid4()}@example.test"
    response = client.post(
        "/api/auth/register",
        json={"email": email, "name": "Test User", "password": password},
    )
    if response.status_code != 201:
        raise AssertionError(f"Test user registration failed: {response.text}")
    return login_as(client, email, password)


def login_as(client: TestClient, email: str, password: str = TEST_PASSWORD) -> dict:
    response = client.post(
        "/api/auth/token", data={"username": email, "password": password}
    )
    if response.status_code != 200:
        raise AssertionError(f"Test user login failed: {response.text}")
    token = response.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client.get("/api/auth/me").json()


def new_authenticated_client(app=None) -> TestClient:
    if app is None:
        from backend.main import app

    client = TestClient(app)
    register_and_login(client)
    return client


@contextmanager
def authenticated_client(app=None):
    client = new_authenticated_client(app)
    with client:
        yield client
