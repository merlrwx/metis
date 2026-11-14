from datetime import UTC, datetime, timedelta

import httpx
import pytest
from metis_web import rendering
from metis_web.backend import error_detail
from metis_web.config import Settings
from metis_web.session import SessionCodec, safe_next


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("relative", "/"),
        ("//evil.test/x", "/"),
        ("/\\evil.test", "/"),
        ("/o/1?x=2", "/o/1?x=2"),
    ],
)
def test_safe_next(value, expected):
    assert safe_next(value) == expected


def test_codec_round_trip_and_rejections():
    codec = SessionCodec("x" * 32)
    token = codec.encode({"a": "access", "r": "refresh"})
    assert codec.decode(token, 60) == {"a": "access", "r": "refresh"}
    assert codec.decode(codec.encode({"a": "only"}), 60) is None
    assert codec.decode(SessionCodec("y" * 32).encode({"a": 1, "r": 2}), 60) is None
    assert codec.decode_flash(token) is None
    assert codec.decode(codec.encode_flash("success", "hi"), 60) is None
    assert codec.decode_flash(codec.encode_flash("error", "x")) == {
        "kind": "error",
        "message": "x",
    }
    assert codec.decode_flash("garbage") is None
    assert not codec.csrf_valid(None, "x") and not codec.csrf_valid("c", None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BACKEND_URL", "file:///etc"),
        ("METIS_COOKIE_SECURE", "sometimes"),
        ("METIS_SESSION_DAYS", "0"),
        ("METIS_PUBLIC_URL", "ftp://x"),
    ],
)
def test_settings_validation(monkeypatch, name, value):
    monkeypatch.setenv("METIS_SESSION_COOKIE_SECRET", "s" * 32)
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError):
        Settings.from_environment()


def test_cookie_secure_setting(monkeypatch):
    monkeypatch.setenv("METIS_SESSION_COOKIE_SECRET", "s" * 32)
    monkeypatch.setenv("METIS_COOKIE_SECURE", "true")
    from fastapi.testclient import TestClient
    from metis_web.app import create_app

    app = create_app(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    )
    with TestClient(app) as client:
        cookie = client.get("/login").headers["set-cookie"]
    assert "secure" in cookie.lower()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(400, json={"detail": "Plain"}), "Plain"),
        (
            httpx.Response(422, json={"detail": [{"msg": "Value error, Bad email"}]}),
            "Bad email",
        ),
        (httpx.Response(429, text="slow down"), "Too many requests"),
        (httpx.Response(502, text="<html>"), "temporarily unavailable"),
        (httpx.Response(418, json=[]), "Request failed (418)."),
    ],
)
def test_error_detail(response, expected):
    assert expected in error_detail(response)


def test_time_filters():
    now = datetime.now(UTC)
    assert rendering.ago(now.isoformat()) == "just now"
    assert rendering.ago((now - timedelta(minutes=5)).isoformat()) == "5 minutes ago"
    assert rendering.ago((now - timedelta(hours=1)).isoformat()) == "1 hour ago"
    assert rendering.ago((now - timedelta(days=3)).isoformat()) == "3 days ago"
    assert rendering.ago(None) == "—"
    assert rendering.when("2026-11-04T09:05:00") == "4 Nov 2026, 09:05 UTC"
    assert rendering.when("not a date", "never") == "never"
    assert rendering.when(datetime(2026, 1, 2, tzinfo=UTC)).startswith("2 Jan 2026")


def test_status_device_and_url_filters():
    assert rendering.status("indexed") == ("Indexed", "ok")
    assert rendering.status(None) == ("Metadata only", "muted")
    assert rendering.status("odd_state") == ("Odd state", "muted")
    assert rendering.device(None) == "Unknown device"
    assert rendering.device("metis-web") == "Metis web app"
    assert rendering.device("curl/8") == "curl"
    assert rendering.device("Something") == "Browser"
    assert (
        rendering.safe_url("https://sharepoint.test/x") == "https://sharepoint.test/x"
    )
    assert rendering.safe_url("javascript:alert(1)") is None
    assert rendering.safe_url("http://[bad") is None


def test_static_assets_are_served_with_security_headers(anon):
    response = anon.get("/static/app.js")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "no-store" not in response.headers.get("cache-control", "")
    assert anon.get("/healthz").text == "ok"


def test_unknown_routes_render_not_found(anon):
    response = anon.get("/nope")
    assert response.status_code == 404
    assert "Not found" in response.text


def test_invalid_form_is_reported(client):
    from conftest import post

    response = post(client, "/organisations", {})
    assert response.status_code == 422
    assert "Check the form" in response.text


def test_request_bodies_are_bounded_before_form_parsing(anon, api):
    from metis_web.limits import BATCH_LIMIT, FILE_LIMIT, FORM_LIMIT, limit_for

    assert limit_for("/login") == FORM_LIMIT
    assert limit_for("/o/x/documents") == BATCH_LIMIT
    assert limit_for("/o/x/documents/y/replace") == FILE_LIMIT
    declared = anon.post("/login", content=b"x" * (FORM_LIMIT + 1))
    assert declared.status_code == 413

    def chunks():
        for _ in range(FORM_LIMIT // 1024 + 2):
            yield b"x" * 1024

    streamed = anon.post(
        "/login",
        content=chunks(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    # FastAPI reports an aborted form read as 400; either way reading stops at the limit.
    assert streamed.status_code in (400, 413)
    assert not api.called("POST", "/api/auth/token")
