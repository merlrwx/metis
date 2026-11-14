import httpx
import pytest
from conftest import ORG, csrf, me, post


def test_login_failure_shows_message_and_keeps_email(anon, api):
    api.on("POST", "/api/auth/token", (401, {"detail": "Incorrect email or password"}))
    page = anon.get("/login")
    response = anon.post(
        "/login",
        data={"email": "a@example.test", "password": "x", "csrf_token": csrf(page)},
    )
    assert response.status_code == 400
    assert "That email and password do not match." in response.text
    assert 'value="a@example.test"' in response.text
    assert "metis_session" not in response.cookies


def test_throttled_login_shows_api_detail(anon, api):
    api.on(
        "POST",
        "/api/auth/token",
        (429, {"detail": "Too many failed sign-in attempts."}),
    )
    page = anon.get("/login")
    response = anon.post(
        "/login",
        data={"email": "a@example.test", "password": "x", "csrf_token": csrf(page)},
    )
    assert "Too many failed sign-in attempts." in response.text


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [("/o/x/chat", "/o/x/chat"), ("https://evil.test/", "/"), ("//evil.test", "/")],
)
def test_login_only_redirects_to_local_paths(anon, submitted, expected):
    page = anon.get("/login")
    response = anon.post(
        "/login",
        data={
            "email": "a@example.test",
            "password": "pw",
            "next": submitted,
            "csrf_token": csrf(page),
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == expected


def test_signed_in_users_skip_login_and_register(client):
    assert client.get("/login", follow_redirects=False).status_code == 303
    assert client.get("/register", follow_redirects=False).status_code == 303


def test_register_signs_in_and_shows_onboarding(anon, api):
    api.on("POST", "/api/auth/register", (201, {"id": "x"}))
    api.on("GET", "/api/auth/me", (200, me(memberships=[])))
    page = anon.get("/register?email=new@example.test")
    assert 'value="new@example.test"' in page.text
    response = anon.post(
        "/register",
        data={
            "name": "New Person",
            "email": "new@example.test",
            "password": "long-enough-password",
            "csrf_token": csrf(page),
        },
    )
    assert response.status_code == 200
    assert "Welcome to Metis, Olive" in response.text
    assert "metis_session" in response.cookies
    assert api.called("POST", "/api/auth/register") == [
        {
            "email": "new@example.test",
            "name": "New Person",
            "password": "long-enough-password",
        }
    ]


def test_register_with_next_redirects(anon, api):
    api.on("POST", "/api/auth/register", (201, {"id": "x"}))
    page = anon.get("/register")
    response = anon.post(
        "/register",
        data={
            "name": "N",
            "email": "n@example.test",
            "password": "long-enough-password",
            "next": "/invite/abc",
            "csrf_token": csrf(page),
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/invite/abc"


def test_register_errors_are_shown(anon, api):
    api.on(
        "POST",
        "/api/auth/register",
        (422, {"detail": [{"msg": "String should have at least 12 characters"}]}),
    )
    page = anon.get("/register")
    response = anon.post(
        "/register",
        data={
            "name": "N",
            "email": "n@x.test",
            "password": "short",
            "csrf_token": csrf(page),
        },
    )
    assert response.status_code == 400
    assert "at least 12 characters" in response.text


def test_logout_flashes_and_clears_session(client, api):
    response = post(client, "/logout", follow_redirects=True)
    assert "You have signed out." in response.text
    assert api.called("POST", "/api/auth/logout")
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_logout_survives_api_outage(client, api):
    api.on("POST", "/api/auth/logout", lambda request: httpx.Response(503))
    response = post(client, "/logout")
    assert response.status_code == 303


def test_htmx_requests_get_hx_redirect_when_session_ends(client, api):
    api.on("GET", "/api/auth/me", (401, {"detail": "expired"}))
    api.on("POST", "/api/auth/refresh", (401, {"detail": "revoked"}))
    response = client.get(
        f"/o/{ORG}", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert response.headers["HX-Redirect"].startswith("/login?next=")


def test_tampered_session_cookie_is_discarded(anon):
    anon.cookies.set("metis_session", "not-a-valid-token")
    response = anon.get("/", follow_redirects=False)
    assert response.headers["location"] == "/login"
    assert 'metis_session=""' in response.headers.get("set-cookie", "")


def test_invitation_page_for_anonymous_visitor(anon, api):
    api.on(
        "POST",
        "/api/invitations/preview",
        (
            200,
            {
                "organisation_name": "Acme",
                "email": "invitee@example.test",
                "role": "admin",
                "expires_at": "2026-11-21T00:00:00Z",
            },
        ),
    )
    page = anon.get("/invite/token-123")
    assert "Join Acme" in page.text
    assert "an <strong>admin</strong>" in page.text
    assert (
        "/register?next=/invite/token-123&amp;email=invitee%40example.test" in page.text
    )


def test_invitation_mismatch_and_accept(client, api):
    preview = {
        "organisation_name": "Acme",
        "email": "someone-else@example.test",
        "role": "member",
        "expires_at": "2026-11-21T00:00:00Z",
    }
    api.on("POST", "/api/invitations/preview", (200, preview))
    assert "Sign out and sign in as" in client.get("/invite/t").text
    preview["email"] = "owner@example.test"
    assert "Accept and join" in client.get("/invite/t").text
    api.on("POST", "/api/invitations/accept", (200, {"organisation_id": ORG}))
    response = post(client, "/invite/t/accept")
    assert response.headers["location"] == f"/o/{ORG}"


def test_unavailable_invitation(anon, api):
    api.on("POST", "/api/invitations/preview", (404, {"detail": "gone"}))
    response = anon.get("/invite/expired")
    assert response.status_code == 404
    assert "Invitation unavailable" in response.text
