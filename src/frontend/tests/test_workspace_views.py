import httpx
from conftest import ORG, document, me, post

BASE = f"/api/organisations/{ORG}"


def test_home_routes_to_remembered_or_first_organisation(client, api):
    other = "00000000-0000-0000-0000-0000000000bb"
    api.on(
        "GET",
        "/api/auth/me",
        (
            200,
            me(
                memberships=[
                    {
                        "organisation_id": ORG,
                        "organisation_name": "Acme",
                        "role": "owner",
                    },
                    {
                        "organisation_id": other,
                        "organisation_name": "Beta",
                        "role": "member",
                    },
                ]
            ),
        ),
    )
    assert client.get("/", follow_redirects=False).headers["location"] == f"/o/{ORG}"
    client.get(f"/o/{other}")
    assert client.get("/", follow_redirects=False).headers["location"] == f"/o/{other}"
    assert "Create an organisation" in client.get("/?new=1").text


def test_create_organisation(client, api):
    api.on("POST", "/api/organisations", (201, {"id": ORG, "name": "Acme"}))
    response = post(client, "/organisations", {"name": "Acme"}, follow_redirects=True)
    assert "Acme is ready" in response.text
    api.on("POST", "/api/organisations", (422, {"detail": "blank"}))
    assert post(client, "/organisations", {"name": " "}).status_code == 400


def test_overview_counts_and_failed_documents(client, api):
    failed = document(
        title="broken.pdf", ingestion_status="failed", ingestion_error="No text"
    )
    api.on(
        "GET",
        f"{BASE}/documents",
        (
            200,
            [
                document(),
                document(ingestion_status="processing"),
                document(ingestion_status="deleted", title="gone.pdf"),
                failed,
            ],
        ),
    )
    page = client.get(f"/o/{ORG}")
    assert "Needs attention" in page.text and "No text" in page.text
    assert "gone.pdf" not in page.text
    assert "Retry" in page.text


def test_unknown_organisation_is_404(client):
    response = client.get("/o/00000000-0000-0000-0000-000000000999")
    assert response.status_code == 404


def test_knowledge_page_polls_only_while_busy_and_filters(client, api):
    api.on("GET", f"{BASE}/documents", (200, [document(title="Leave policy.pdf")]))
    page = client.get(f"/o/{ORG}/knowledge?q=leave")
    assert "Leave policy.pdf" in page.text and 'hx-trigger="every 4s"' not in page.text
    assert "No matches" in client.get(f"/o/{ORG}/knowledge?q=zzz").text
    api.on("GET", f"{BASE}/documents", (200, [document(ingestion_status="queued")]))
    assert 'hx-trigger="every 4s"' in client.get(f"/o/{ORG}/knowledge").text


def test_members_see_read_only_knowledge(client, api):
    api.role = "member"
    page = client.get(f"/o/{ORG}/knowledge")
    assert 'id="upload"' not in page.text
    assert "owner or admin can upload" in page.text
    response = post(
        client,
        f"/o/{ORG}/documents",
        files={"files": ("a.txt", b"x", "text/plain")},
    )
    assert response.status_code == 403


def test_upload_batch_reports_each_file(client, api):
    def upload(request):
        if b"bad.txt" in request.content:
            return httpx.Response(415, json={"detail": "Unsupported file type"})
        return httpx.Response(
            201, json={"document": {"title": "good.txt"}, "job": {"status": "queued"}}
        )

    api.on("POST", f"{BASE}/documents/upload", upload)
    response = post(
        client,
        f"/o/{ORG}/documents",
        {"conflict_action": "bogus"},
        files=[
            ("files", ("good.txt", b"hello", "text/plain")),
            ("files", ("bad.txt", b"x", "text/plain")),
        ],
    )
    assert response.status_code == 200
    assert "<strong>good.txt</strong> uploaded" in response.text
    assert "Unsupported file type" in response.text
    sent = [call for call in api.calls if call[1] == f"{BASE}/documents/upload"]
    assert len(sent) == 2


def test_upload_validation(client):
    assert post(client, f"/o/{ORG}/documents").status_code == 400
    files = [("files", (f"{index}.txt", b"x", "text/plain")) for index in range(11)]
    response = post(client, f"/o/{ORG}/documents", files=files)
    assert response.status_code == 400 and "at most 10" in response.text


def test_document_detail_and_actions(client, api):
    doc = document(ingestion_status="failed", ingestion_error="Scanned PDF")
    version = doc["current_version_id"]
    api.on(
        "GET",
        f"{BASE}/documents/{doc['id']}/detail",
        (
            200,
            {
                "document": doc,
                "versions": [
                    {
                        "id": version,
                        "filename": "policy.pdf",
                        "created_at": doc["created_at"],
                        "current": True,
                    }
                ],
                "versions_truncated": False,
                "extraction_preview": "Policy text",
                "preview_truncated": True,
                "page_count": 2,
                "chunk_count": 5,
                "last_successful_indexing": None,
                "source_modified_at": None,
                "visibility": "Shared with all members of this organisation",
                "readiness": "Ready means searchable.",
            },
        ),
    )
    page = client.get(f"/o/{ORG}/documents/{doc['id']}")
    assert "Policy text" in page.text and "first 4,000 characters" in page.text
    assert "Retry ingestion" in page.text and "Scanned PDF" in page.text
    path = f"/o/{ORG}/documents/{doc['id']}"
    assert post(client, f"{path}/rename", {"title": "New"}).status_code == 303
    assert api.called("PATCH", f"{BASE}/documents/{doc['id']}") == [{"title": "New"}]
    post(client, f"{path}/reindex")
    assert api.called("POST", f"{BASE}/documents/{doc['id']}/reindex")
    for back, target in (
        ("overview", f"/o/{ORG}"),
        ("knowledge", f"/o/{ORG}/knowledge"),
    ):
        assert (
            post(client, f"{path}/retry", {"back": back}).headers["location"] == target
        )
    removed = post(client, f"{path}/remove")
    assert removed.headers["location"] == f"/o/{ORG}/knowledge"
    api.on(
        "PATCH", f"{BASE}/documents/{doc['id']}", (422, {"detail": "Title too long"})
    )
    flashed = post(client, f"{path}/rename", {"title": "x"}, follow_redirects=True)
    assert "Title too long" in flashed.text


def test_replace_document(client, api):
    path = f"/o/{ORG}/documents/{ORG}"
    api.on(
        "POST",
        f"{BASE}/documents/upload",
        (201, {"document": {"title": "v2.pdf"}, "job": {"status": "queued"}}),
    )
    ok = post(
        client,
        f"{path}/replace",
        files={"file": ("v2.pdf", b"%PDF", "application/pdf")},
    )
    assert ok.headers["location"] == path
    api.on("POST", f"{BASE}/documents/upload", (413, {"detail": "Too large"}))
    api.on(
        "GET",
        f"{BASE}/documents/{ORG}/detail",
        (
            200,
            {
                "document": document(id=ORG),
                "versions": [],
                "extraction_preview": "",
                "preview_truncated": False,
                "page_count": 0,
                "chunk_count": 0,
                "visibility": "Shared",
                "readiness": "Ready",
            },
        ),
    )
    failed = post(
        client,
        f"{path}/replace",
        files={"file": ("v2.pdf", b"%PDF", "application/pdf")},
        follow_redirects=True,
    )
    assert "Too large" in failed.text


def test_download_is_always_an_attachment(client, api):
    path = f"{BASE}/documents/d/versions/v/download"
    api.on(
        "GET",
        path.replace("/d/", "/[^/]+/").replace("/v/", "/[^/]+/"),
        lambda request: httpx.Response(
            200,
            content=b"bytes",
            headers={"content-disposition": "inline; filename=x.html"},
        ),
    )
    response = client.get(f"/o/{ORG}/documents/{ORG}/versions/{ORG}/download")
    assert response.content == b"bytes"
    assert response.headers["content-disposition"] == "attachment"
    assert response.headers["content-type"] == "application/octet-stream"


def test_sources_and_groups(client, api):
    target = f"/o/{ORG}/knowledge"
    unapproved = post(
        client,
        f"{target[:-10]}/sources",
        {"name": "Lib", "kind": "microsoft365"},
        follow_redirects=True,
    )
    assert "Confirm organisation-wide library approval" in unapproved.text
    post(
        client,
        f"/o/{ORG}/sources",
        {
            "name": "Lib",
            "kind": "microsoft365",
            "approved": "true",
            "auto_sync": "true",
        },
    )
    assert api.called("POST", f"{BASE}/sources")[-1] == {
        "name": "Lib",
        "type": "microsoft365",
        "organisation_library_approved": True,
        "configuration": {"sync_enabled": True},
    }
    source = f"/o/{ORG}/sources/{ORG}"
    api.on("POST", f"{BASE}/sources/{ORG}/sync", (202, {"sync_status": "syncing"}))
    assert "in progress" in post(client, f"{source}/sync", follow_redirects=True).text
    api.on(
        "POST",
        f"{BASE}/sources/{ORG}/check",
        (200, {"connected": True, "library_name": "HR"}),
    )
    assert (
        "Connection confirmed · HR"
        in post(client, f"{source}/check", follow_redirects=True).text
    )
    api.on("POST", f"{BASE}/sources/{ORG}/check", (409, {"detail": "Source is busy"}))
    assert (
        "Source is busy" in post(client, f"{source}/check", follow_redirects=True).text
    )
    post(client, f"{source}/settings", {"paused": "true"})
    assert api.called("PATCH", f"{BASE}/sources/{ORG}") == [
        {"sync_enabled": False, "paused": True}
    ]
    post(client, f"{source}/disconnect")
    assert api.calls[-1][:2] == ("DELETE", f"{BASE}/sources/{ORG}")
    post(client, f"/o/{ORG}/groups", {"name": "HR", "source_ids": [ORG]})
    assert api.called("POST", f"{BASE}/groups") == [{"name": "HR", "source_ids": [ORG]}]
    post(client, f"/o/{ORG}/groups/{ORG}", {"name": "HR2"})
    assert api.called("PUT", f"{BASE}/groups/{ORG}") == [
        {"name": "HR2", "source_ids": []}
    ]
    post(client, f"/o/{ORG}/groups/{ORG}/delete")
    assert api.calls[-1][:2] == ("DELETE", f"{BASE}/groups/{ORG}")


def test_server_errors_render_error_page(client, api):
    api.on("GET", f"{BASE}/documents", (500, {"detail": "boom"}))
    response = client.get(f"/o/{ORG}")
    assert response.status_code == 500
    assert (
        "Metis is temporarily unavailable" in response.text or "boom" in response.text
    )


def test_api_unreachable(client, api):
    def fail(request):
        raise httpx.ConnectError("down")

    api.on("GET", f"{BASE}/documents", fail)
    response = client.get(f"/o/{ORG}")
    assert response.status_code == 503


def test_document_preview_and_warnings_are_admin_only(client, api):
    doc = document()
    detail = {
        "document": doc,
        "versions": [],
        "extraction_preview": "Secret text",
        "preview_truncated": False,
        "page_count": 2,
        "chunk_count": 3,
        "extracted_characters": 12345,
        "blank_page_count": 1,
        "parsing_warnings": ["Page 2 has no extractable text."],
        "visibility": "Shared",
        "readiness": "Ready",
    }
    api.on("GET", f"{BASE}/documents/{doc['id']}/detail", (200, detail))
    admin_page = client.get(f"/o/{ORG}/documents/{doc['id']}")
    assert (
        "12,345" in admin_page.text
        and "Page 2 has no extractable text." in admin_page.text
    )
    api.role = "member"
    member_detail = {
        **detail,
        "extraction_preview": "",
        "extracted_characters": None,
        "parsing_warnings": [],
    }
    api.on("GET", f"{BASE}/documents/{doc['id']}/detail", (200, member_detail))
    member_page = client.get(f"/o/{ORG}/documents/{doc['id']}")
    assert "available to organisation owners and admins" in member_page.text
    assert "Extracted characters" not in member_page.text
