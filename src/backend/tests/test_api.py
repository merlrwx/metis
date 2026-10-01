from backend.main import app, main
from fastapi.testclient import TestClient


def test_health_check():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_application_identity():
    with TestClient(app) as client:
        response = client.get("/api/info")
    assert response.status_code == 200
    assert response.json()["name"] == "Metis"
    assert response.json()["stage"] == "async-processing"


def test_timer_domain_removed():
    with TestClient(app) as client:
        for path in ("/api/timer", "/api/sessions"):
            assert client.get(path).status_code == 404


def test_entrypoint_uses_configured_address(monkeypatch):
    calls = []
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setattr(
        "backend.main.uvicorn.run", lambda *a, **kw: calls.append((a, kw))
    )
    main()
    assert calls == [((app,), {"host": "127.0.0.1", "port": 9000})]
