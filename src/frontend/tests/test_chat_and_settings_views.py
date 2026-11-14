import httpx
from conftest import ORG, USER, document, post

BASE = f"/api/organisations/{ORG}"
CONV = "00000000-0000-0000-0000-0000000000cc"
MSG = "00000000-0000-0000-0000-0000000000dd"
U2 = "00000000-0000-0000-0000-0000000000e2"
INV = "00000000-0000-0000-0000-0000000000f1"
S2 = "00000000-0000-0000-0000-0000000000f2"


def conversation(content="**Report** within 24 hours [C1].", citations=None):
    return {
        "id": CONV,
        "organisation_id": ORG,
        "title": "Incidents",
        "created_at": "2026-11-14T00:00:00Z",
        "scope": {"document_id": "11111111-1111-1111-1111-111111111111"},
        "messages": [
            {
                "id": "u1",
                "role": "user",
                "content": "<b>What</b> do we do?",
                "citations": [],
                "model_id": None,
                "input_tokens": None,
                "output_tokens": None,
                "created_at": "2026-11-14T00:00:00Z",
            },
            {
                "id": MSG,
                "role": "assistant",
                "content": content,
                "outcome": "partially_answered",
                "citations": citations
                if citations is not None
                else [
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "document_title": "Incident policy",
                        "document_version_id": "v1",
                        "page": 3,
                        "section": "Reporting",
                        "source_name": "HR",
                        "source_type": "microsoft365",
                        "source_freshness": "stale",
                        "source_url": "javascript:alert(1)",
                        "snippet": "Report incidents within 24 hours.",
                    }
                ],
                "model_id": "m",
                "input_tokens": 1,
                "output_tokens": 1,
                "created_at": "2026-11-14T00:00:00Z",
            },
        ],
    }


def test_chat_landing_page(client, api):
    page = client.get(f"/o/{ORG}/chat?q=Hello")
    assert "Ask anything about Acme" in page.text
    assert ">Hello</textarea>" in page.text
    assert "No documents are ready yet." in page.text


def test_conversation_renders_safe_markdown_and_citations(client, api):
    api.on(
        "GET",
        f"{BASE}/conversations/{CONV}",
        (
            200,
            conversation(
                content="**Report** <script>alert(1)</script> [x](javascript:alert(1))"
            ),
        ),
    )
    api.on(
        "GET",
        f"{BASE}/conversations",
        (
            200,
            [
                {
                    "id": CONV,
                    "title": "Incidents",
                    "created_at": "2026-11-14T00:00:00Z",
                    "scope": {},
                }
            ],
        ),
    )
    page = client.get(f"/o/{ORG}/chat/{CONV}")
    assert "<strong>Report</strong>" in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert 'href="javascript:' not in page.text
    assert "&lt;b&gt;What&lt;/b&gt;" in page.text
    assert "Incident policy" in page.text and "p. 3" in page.text
    assert "stored snapshot" in page.text
    assert "Partially answered" in page.text
    assert "Unavailable document" in page.text
    assert 'aria-current="page"' in page.text


def test_no_citations_message(client, api):
    api.on("GET", f"{BASE}/conversations/{CONV}", (200, conversation(citations=[])))
    assert (
        "No supporting sources were found" in client.get(f"/o/{ORG}/chat/{CONV}").text
    )


def test_ask_success_redirects_to_conversation(client, api):
    api.on(
        "GET",
        f"{BASE}/documents",
        (200, [document(id="11111111-1111-1111-1111-111111111111")]),
    )
    api.on(
        "POST",
        f"{BASE}/chat",
        (
            200,
            {
                "conversation_id": CONV,
                "message_id": MSG,
                "related_passages": [
                    {
                        "document_title": "Rota guide",
                        "snippet": "Nearby evidence",
                        "page": 2,
                    }
                ],
            },
        ),
    )
    api.on("GET", f"{BASE}/conversations/{CONV}", (200, conversation()))
    request_id = "22222222-2222-2222-2222-222222222222"
    response = post(
        client,
        f"/o/{ORG}/chat",
        {
            "message": " What now? ",
            "request_id": request_id,
            "group_id": ORG,
            "source_ids": ["s1"],
            "document_ids": ["11111111-1111-1111-1111-111111111111"],
        },
    )
    assert response.status_code == 200
    assert response.headers["HX-Push-Url"] == f"/o/{ORG}/chat/{CONV}#latest"
    assert "Related retrieved passages · 1" in response.text
    assert "Nearby evidence" in response.text
    assert "review the related passages" in response.text
    assert api.called("POST", f"{BASE}/chat") == [
        {
            "message": "What now?",
            "conversation_id": None,
            "request_id": request_id,
            "group_id": ORG,
            "document_ids": ["11111111-1111-1111-1111-111111111111"],
        }
    ]


def test_ask_failure_keeps_question_and_request_id(client, api):
    api.on("POST", f"{BASE}/chat", (503, {"detail": "Chat model unavailable"}))
    request_id = "33333333-3333-3333-3333-333333333333"
    response = post(
        client,
        f"/o/{ORG}/chat",
        {"message": "Keep me", "request_id": request_id, "conversation_id": CONV},
    )
    assert response.status_code == 200
    assert "answer provider is unavailable" in response.text
    assert ">Keep me</textarea>" in response.text
    assert f'value="{request_id}"' in response.text


def test_ask_validation_and_bad_request_id(client, api):
    assert post(client, f"/o/{ORG}/chat", {"message": "  "}).status_code == 400
    api.on("POST", f"{BASE}/chat", (200, {"conversation_id": CONV}))
    api.on("GET", f"{BASE}/conversations/{CONV}", (200, conversation()))
    post(client, f"/o/{ORG}/chat", {"message": "Hi", "request_id": "nope"})
    assert len(api.called("POST", f"{BASE}/chat")[0]["request_id"]) == 36


def test_conversation_management_and_feedback(client, api):
    path = f"/o/{ORG}/chat/{CONV}"
    post(client, f"{path}/rename", {"title": "Renamed"})
    assert api.called("PATCH", f"{BASE}/conversations/{CONV}") == [{"title": "Renamed"}]
    assert post(client, f"{path}/delete").headers["location"] == f"/o/{ORG}/chat"
    feedback = post(
        client,
        f"{path}/messages/{MSG}/feedback",
        {"vote": "problem", "reason": "made_up", "comment": "  wrong page "},
    )
    assert feedback.headers["location"].endswith(f"#message-{MSG}")
    assert api.called(
        "PUT", f"{BASE}/conversations/{CONV}/messages/{MSG}/feedback"
    ) == [{"vote": "problem", "reason": None, "comment": "wrong page"}]


def members():
    return [
        {
            "user_id": USER,
            "email": "owner@example.test",
            "name": "Olive Owner",
            "role": "owner",
            "created_at": "2026-11-14T00:00:00Z",
        },
        {
            "user_id": U2,
            "email": "m@example.test",
            "name": "Max Member",
            "role": "member",
            "created_at": "2026-11-14T00:00:00Z",
        },
    ]


def test_settings_for_owner(client, api):
    api.on("GET", f"{BASE}/members", (200, members()))
    api.on(
        "GET",
        f"{BASE}/invitations",
        (
            200,
            [
                {
                    "id": "i1",
                    "organisation_id": ORG,
                    "email": "new@example.test",
                    "role": "admin",
                    "created_at": "2026-11-14T00:00:00Z",
                    "expires_at": "2026-11-21T00:00:00Z",
                }
            ],
        ),
    )
    page = client.get(f"/o/{ORG}/settings")
    assert "Max Member" in page.text and "new@example.test" in page.text
    assert "Only owner" in page.text
    assert "You are the only owner" in page.text
    assert 'value="admin">Admin — can also manage' in page.text


def test_settings_for_member(client, api):
    api.role = "member"
    page = client.get(f"/o/{ORG}/settings")
    assert "Invite people" not in page.text
    assert "Leave Acme" in page.text
    assert not api.called("GET", f"{BASE}/members")


def test_settings_actions(client, api):
    path = f"/o/{ORG}/settings"
    post(client, f"{path}/name", {"name": "Acme Ltd"})
    assert api.called("PATCH", BASE) == [{"name": "Acme Ltd"}]
    post(client, f"{path}/members", {"email": "x@example.test", "role": "admin"})
    assert api.called("POST", f"{BASE}/members") == [
        {"email": "x@example.test", "role": "admin"}
    ]
    post(client, f"{path}/members/{U2}/role", {"role": "admin"})
    assert api.called("PATCH", f"{BASE}/members/{U2}") == [{"role": "admin"}]
    assert (
        post(client, f"{path}/members/{U2}/remove").headers["location"]
        == f"{path}#members"
    )
    assert post(client, f"{path}/members/{USER}/remove").headers["location"] == "/"
    api.on(
        "DELETE",
        f"{BASE}/members/{USER}",
        (409, {"detail": "An organisation needs at least one owner"}),
    )
    blocked = post(client, f"{path}/members/{USER}/remove", follow_redirects=True)
    assert "at least one owner" in blocked.text
    post(client, f"{path}/invitations/{INV}/revoke")
    assert api.calls[-1][:2] == ("DELETE", f"{BASE}/invitations/{INV}")


def test_invitation_link_is_shown_once(client, api, monkeypatch):
    api.on(
        "POST",
        f"{BASE}/invitations",
        (
            201,
            {
                "id": "i1",
                "organisation_id": ORG,
                "email": "new@example.test",
                "role": "member",
                "created_at": "2026-11-14T00:00:00Z",
                "expires_at": "2026-11-21T00:00:00Z",
                "token": "secret-token",
            },
        ),
    )
    page = post(client, f"/o/{ORG}/settings/invitations", {"email": "new@example.test"})
    assert page.status_code == 200
    assert 'value="http://testserver/invite/secret-token"' in page.text
    api.on("POST", f"{BASE}/invitations", (409, {"detail": "User is already a member"}))
    flashed = post(
        client,
        f"/o/{ORG}/settings/invitations",
        {"email": "m@example.test"},
        follow_redirects=True,
    )
    assert "already a member" in flashed.text


def test_account_page_and_actions(client, api):
    api.on(
        "GET",
        "/api/auth/sessions",
        (
            200,
            [
                {
                    "id": "s1",
                    "created_at": "2026-11-14T00:00:00Z",
                    "last_used_at": "2026-11-14T00:00:00Z",
                    "expires_at": "2026-12-14T00:00:00Z",
                    "user_agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0",
                    "ip_address": "10.0.0.5",
                    "current": True,
                },
                {
                    "id": S2,
                    "created_at": "2026-11-14T00:00:00Z",
                    "last_used_at": "2026-11-14T00:00:00Z",
                    "expires_at": "2026-12-14T00:00:00Z",
                    "user_agent": "Mozilla/5.0 (iPhone) Safari",
                    "ip_address": None,
                    "current": False,
                },
            ],
        ),
    )
    page = client.get("/account")
    assert "This device" in page.text and "Firefox on Linux" in page.text
    assert "Safari on iOS" in page.text and "10.0.0.5" not in page.text
    post(client, "/account/profile", {"name": "Olive"})
    assert api.called("PATCH", "/api/auth/me") == [{"name": "Olive"}]
    mismatch = post(
        client,
        "/account/password",
        {
            "current_password": "a",
            "new_password": "b" * 12,
            "confirm_password": "c" * 12,
        },
        follow_redirects=True,
    )
    assert "The new passwords differ." in mismatch.text
    post(
        client,
        "/account/password",
        {
            "current_password": "a",
            "new_password": "b" * 12,
            "confirm_password": "b" * 12,
        },
    )
    assert api.called("POST", "/api/auth/password") == [
        {"current_password": "a", "new_password": "b" * 12}
    ]
    post(client, f"/account/sessions/{S2}/revoke")
    assert api.calls[-1][:2] == ("DELETE", f"/api/auth/sessions/{S2}")
    post(client, "/account/sessions/revoke-others")
    assert api.called("POST", "/api/auth/sessions/revoke-others")


def test_concurrent_refreshes_share_one_rotation(app, api):
    import asyncio

    from metis_web.backend import RefreshCoordinator

    refreshes = []

    def refresh(request):
        refreshes.append(request)
        return httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2"})

    api.on("POST", "/api/auth/refresh", refresh)
    coordinator = RefreshCoordinator()

    async def run():
        client = httpx.AsyncClient(
            base_url="http://backend.test", transport=httpx.MockTransport(api)
        )
        results = await asyncio.gather(
            *(coordinator.refresh(client, "r1") for _ in range(5))
        )
        await client.aclose()
        return results

    results = asyncio.run(run())
    assert len(refreshes) == 1
    assert all(result["refresh_token"] == "r2" for result in results)


import pytest


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (404, "Not found", "A selected source or conversation is unavailable"),
        (409, "Index is being rebuilt", "Knowledge indexing is in progress"),
        (409, "Conversation changed", "Conversation changed"),
        (503, "down", "The embedding or answer provider is unavailable"),
    ],
)
def test_chat_errors_get_specific_guidance(client, api, status, detail, expected):
    api.on("POST", f"{BASE}/chat", (status, {"detail": detail}))
    response = post(client, f"/o/{ORG}/chat", {"message": "Q"})
    assert expected in response.text


def test_chat_warns_about_indexing_and_guides_outcomes(client, api):
    api.on(
        "GET",
        f"{BASE}/documents",
        (
            200,
            [document(ingestion_status="failed"), document(ingestion_status="queued")],
        ),
    )
    insufficient = conversation()
    insufficient["messages"][1]["outcome"] = "insufficient_evidence"
    api.on("GET", f"{BASE}/conversations/{CONV}", (200, insufficient))
    page = client.get(f"/o/{ORG}/chat/{CONV}")
    assert "1 document failed indexing" in page.text
    assert "1 document still being indexed" in page.text
    assert "Try a narrower question" in page.text
    assert "freshness: stale" in page.text
    assert "Preview extracted text" in page.text
    assert "Related retrieved passages" not in page.text
