import io
import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"
ORG_ID = "11111111-1111-4111-8111-111111111111"
DOC_ID = "22222222-2222-4222-8222-222222222222"
SOURCE_ID = "33333333-3333-4333-8333-333333333333"


def user_response(*, memberships=None):
    return {
        "id": "user-1",
        "email": "owner@example.test",
        "name": "Owner",
        "created_at": "2026-01-01T00:00:00Z",
        "memberships": memberships
        if memberships is not None
        else [
            {
                "organisation_id": ORG_ID,
                "organisation_name": "North Clinic",
                "role": "owner",
            }
        ],
    }


def document_response(status="failed"):
    return [
        {
            "id": DOC_ID,
            "title": "Medication policy",
            "ingestion_status": status,
            "ingestion_error": "Text extraction failed" if status == "failed" else None,
            "updated_at": "2026-09-01T12:00:00Z",
        }
    ]


def backend_response(request, timeout=30, *, documents=None, memberships=None):
    path = urlparse(request.full_url).path
    method = request.get_method()
    if path == "/api/auth/me":
        body = user_response(memberships=memberships)
    elif path == "/api/auth/register" and method == "POST":
        body = {"id": "user-1"}
    elif path == "/api/auth/token" and method == "POST":
        body = {"access_token": "signed-token"}
    elif path == "/api/organisations" and method == "POST":
        body = {"id": ORG_ID, "name": "North Clinic"}
    elif path == f"/api/organisations/{ORG_ID}/documents":
        body = document_response() if documents is None else documents
    elif path == f"/api/organisations/{ORG_ID}/sources" and method == "GET":
        body = [
            {
                "id": SOURCE_ID,
                "name": "Policies",
                "type": "upload",
                "configuration": {},
            }
        ]
    elif (
        path == f"/api/organisations/{ORG_ID}/conversations"
        and method == "GET"
        or path == f"/api/organisations/{ORG_ID}/groups"
        and method == "GET"
    ):
        body = []
    elif (
        path == f"/api/organisations/{ORG_ID}/conversations/conversation-1"
        and method == "GET"
    ):
        body = {
            "id": "conversation-1",
            "scope": {},
            "messages": [
                {"role": "user", "content": "What should staff do?"},
                {
                    "role": "assistant",
                    "content": "Follow the medication policy.",
                    "citations": [],
                },
            ],
        }
    elif path == f"/api/organisations/{ORG_ID}/members":
        body = (
            [
                {
                    "name": "Owner",
                    "email": "owner@example.test",
                    "role": "owner",
                }
            ]
            if method == "GET"
            else {"email": "member@example.test", "role": "member"}
        )
    elif path == f"/api/organisations/{ORG_ID}/sources" and method == "POST":
        body = {"id": SOURCE_ID, "name": "Policies", "type": "upload"}
    elif path == f"/api/organisations/{ORG_ID}/chat" and method == "POST":
        body = {
            "conversation_id": "conversation-1",
            "answer": "Follow the medication policy.",
            "citations": [],
        }
    elif path.endswith("/documents/upload") and method == "POST":
        body = {
            "document": {"title": "Uploaded policy"},
            "job": {"status": "queued"},
        }
    elif path.endswith("/retry") and method == "POST":
        body = {"id": "job-1", "status": "queued"}
    elif path == "/api/info":
        body = {"name": "Metis", "stage": "grounded-chat"}
    else:
        raise AssertionError(f"Unexpected API request: {method} {path}")
    return io.BytesIO(json.dumps(body).encode())


def authenticated_app(page="Dashboard", *, documents=None):
    def open_request(request, timeout):
        return backend_response(request, documents=documents)

    app = AppTest.from_file(str(APP))
    app.session_state["metis_token"] = "signed-token"
    with patch("api_client.urlopen", side_effect=open_request):
        app.run()
        if page != "Dashboard":
            app.sidebar.radio[0].set_value(page)
            app.run()
    return app


def test_login_screen_checks_backend_connection():
    request_args = []

    def open_request(request, timeout):
        request_args.append((request, timeout))
        return io.BytesIO(b'{"name":"Metis","stage":"grounded-chat"}')

    with patch("api_client.urlopen", side_effect=open_request):
        app = AppTest.from_file(str(APP)).run()

    assert not app.exception
    assert app.title[0].value == "Metis"
    assert app.success[0].value == "Metis API connected"
    request, timeout = request_args[0]
    assert request.full_url == "http://localhost:8000/api/info"
    assert timeout == 30


@pytest.mark.parametrize("failure", [URLError("offline"), TimeoutError()])
def test_backend_unavailable_is_visible(failure):
    with patch("api_client.urlopen", side_effect=failure):
        app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert "Cannot connect" in app.error[0].value
    assert not app.success


def test_non_metis_backend_is_rejected():
    with patch("api_client.urlopen", return_value=io.BytesIO(b'{"name":"Other"}')):
        app = AppTest.from_file(str(APP)).run()

    assert not app.exception
    assert "not a Metis API" in app.error[0].value


def test_dashboard_shows_failed_ingestion_and_retry():
    documents = document_response() + [
        {
            "id": "ready-doc",
            "title": "Ready policy",
            "ingestion_status": "indexed",
            "updated_at": "2026-09-02T12:00:00Z",
        },
        {
            "id": "pending-doc",
            "title": "Pending policy",
            "ingestion_status": "queued",
            "updated_at": "2026-09-03T12:00:00Z",
        },
        {
            "id": "metadata-doc",
            "title": "Metadata policy",
            "ingestion_status": None,
            "updated_at": "2026-09-04T12:00:00Z",
        },
    ]
    app = authenticated_app(documents=documents)

    assert not app.exception
    assert any(item.value == "Dashboard" for item in app.title)
    assert any("Failed jobs" in item.value for item in app.subheader)
    assert any("Medication policy" in item.value for item in app.markdown)
    assert any(button.label == "Retry" for button in app.button)


def test_knowledge_page_lists_sources_and_upload_form():
    app = authenticated_app("Knowledge")

    assert not app.exception
    assert any(item.value == "Knowledge" for item in app.title)
    assert any("Policies" in item.value for item in app.markdown)
    assert any(button.label == "Upload and process" for button in app.button)
    assert any(
        uploader.label.startswith("Choose a PDF") for uploader in app.file_uploader
    )


def test_chat_page_shows_saved_citations():
    app = AppTest.from_file(str(APP))
    app.session_state["metis_token"] = "signed-token"
    app.session_state["active_organisation"] = ORG_ID
    app.session_state["metis_chat_org"] = ORG_ID
    app.session_state["metis_chat_messages"] = [
        {
            "role": "assistant",
            "content": "Follow the incident policy.",
            "citations": [
                {
                    "document_title": "Medication policy",
                    "source_name": "Policies",
                    "page": 3,
                    "section": "Response",
                    "snippet": "Notify the supervisor.",
                }
            ],
        }
    ]
    with patch("api_client.urlopen", side_effect=backend_response):
        app.run()
        app.sidebar.radio[0].set_value("Chat")
        app.run()

    assert not app.exception
    assert any(item.value == "Chat" for item in app.title)
    assert any("Follow the incident policy." in item.value for item in app.markdown)
    assert any("Medication policy" in item.label for item in app.expander)


def test_settings_page_lists_organisation_members():
    app = authenticated_app("Settings")

    assert not app.exception
    assert any(item.value == "Settings" for item in app.title)
    assert any("North Clinic" in item.value for item in app.markdown)
    assert any("owner@example.test" in str(row.value) for row in app.dataframe)


def test_sign_in_and_account_registration():
    app = AppTest.from_file(str(APP))
    with patch("api_client.urlopen", side_effect=backend_response):
        app.run()
        app.text_input(key="register_name").set_value("New User")
        app.text_input(key="register_email").set_value("new@example.test")
        app.text_input(key="register_password").set_value("long-local-password")
        next(
            button for button in app.button if button.label == "Create account"
        ).click()
        app.run()

    assert not app.exception
    assert any(item.value == "Dashboard" for item in app.title)
    assert app.session_state["metis_token"] == "signed-token"


def test_first_sign_in_can_create_an_organisation():
    no_memberships = []

    def open_request(request, timeout):
        return backend_response(request, memberships=no_memberships)

    app = AppTest.from_file(str(APP))
    app.session_state["metis_token"] = "signed-token"
    with patch("api_client.urlopen", side_effect=open_request):
        app.run()
        assert any(item.value == "Create your organisation" for item in app.title)
        app.text_input(key="new_organisation_name").set_value("North Clinic")
        next(
            button for button in app.button if button.label == "Create organisation"
        ).click()
        app.run()

    assert not app.exception
    assert any("Organisation created" in item.value for item in app.success)


def test_upload_form_explains_when_no_file_is_selected():
    app = authenticated_app("Knowledge")
    with patch("api_client.urlopen", side_effect=backend_response):
        next(
            button for button in app.button if button.label == "Upload and process"
        ).click()
        app.run()

    assert any("Choose a document before uploading" in item.value for item in app.error)


def test_upload_form_sends_file_and_shows_processing_status():
    app = authenticated_app("Knowledge")
    requests = []
    app.file_uploader[0].set_value(("policy.txt", b"Policy content", "text/plain"))

    def open_request(request, timeout):
        requests.append(
            (request.get_method(), urlparse(request.full_url).path, request.data)
        )
        return backend_response(request)

    with patch("api_client.urlopen", side_effect=open_request):
        app.run()
        next(
            button for button in app.button if button.label == "Upload and process"
        ).click()
        app.run()

    uploads = [item for item in requests if item[1].endswith("/documents/upload")]
    assert len(uploads) == 1
    assert uploads[0][0] == "POST"
    assert b"Policy content" in uploads[0][2]
    assert any("Uploaded policy is processing" in item.value for item in app.success)


def test_chat_sends_question_and_renders_answer():
    app = authenticated_app("Chat")
    app.chat_input[0].set_value("What should staff do?")
    with patch("api_client.urlopen", side_effect=backend_response):
        app.run()

    assert not app.exception
    assert app.session_state["metis_chat_id"] == "conversation-1"
    assert any("Follow the medication policy" in item.value for item in app.markdown)


def test_adding_member_uses_selected_role():
    app = authenticated_app("Settings")
    app.text_input[-1].set_value("member@example.test")
    next(button for button in app.button if button.label == "Add member").click()
    requests = []

    def open_request(request, timeout):
        requests.append((request.get_method(), urlparse(request.full_url).path))
        return backend_response(request)

    with patch("api_client.urlopen", side_effect=open_request):
        app.run()

    assert ("POST", f"/api/organisations/{ORG_ID}/members") in requests


def test_dashboard_retry_requeues_failed_ingestion():
    app = authenticated_app()
    requests = []

    def open_request(request, timeout):
        requests.append((request.get_method(), urlparse(request.full_url).path))
        return backend_response(request)

    with patch("api_client.urlopen", side_effect=open_request):
        next(button for button in app.button if button.label == "Retry").click()
        app.run()

    assert ("POST", f"/api/organisations/{ORG_ID}/documents/{DOC_ID}/retry") in requests
    assert any("Ingestion queued again" in item.value for item in app.success)


def test_owner_can_create_a_knowledge_source():
    app = authenticated_app("Knowledge")
    app.text_input[-1].set_value("Incident reports")
    requests = []

    def open_request(request, timeout):
        requests.append((request.get_method(), urlparse(request.full_url).path))
        return backend_response(request)

    with patch("api_client.urlopen", side_effect=open_request):
        next(button for button in app.button if button.label == "Add source").click()
        app.run()

    assert ("POST", f"/api/organisations/{ORG_ID}/sources") in requests
    assert any("Source added" in item.value for item in app.success)


def test_expired_session_returns_to_sign_in():
    def open_request(request, timeout):
        if urlparse(request.full_url).path == "/api/auth/me":
            raise HTTPError(
                request.full_url,
                401,
                "Expired",
                {},
                io.BytesIO(b'{"detail":"Expired"}'),
            )
        return backend_response(request)

    app = AppTest.from_file(str(APP))
    app.session_state["metis_token"] = "expired-token"
    app.session_state["active_organisation"] = ORG_ID
    with patch("api_client.urlopen", side_effect=open_request):
        app.run()

    assert not app.exception
    assert "metis_token" not in app.session_state
    assert any("session expired" in item.value.lower() for item in app.warning)


def test_owner_can_add_microsoft365_source_with_periodic_sync():
    app = authenticated_app("Knowledge")
    submitted = []

    def open_request(request, timeout):
        if request.get_method() == "POST" and urlparse(request.full_url).path.endswith(
            "/sources"
        ):
            submitted.append(json.loads(request.data))
        return backend_response(request)

    with patch("api_client.urlopen", side_effect=open_request):
        next(
            widget for widget in app.selectbox if widget.label == "Source type"
        ).set_value("Microsoft 365 library")
        app.run()
        app.text_input[-1].set_value("Organisation library")
        app.checkbox[0].check()
        next(button for button in app.button if button.label == "Add source").click()
        app.run()
    assert not app.exception
    assert submitted == [
        {
            "name": "Organisation library",
            "type": "microsoft365",
            "configuration": {"sync_enabled": True},
        }
    ]


def test_microsoft365_source_sync_shows_progress_and_can_retry():
    requests = []

    def open_request(request, timeout):
        path = urlparse(request.full_url).path
        if (
            path == f"/api/organisations/{ORG_ID}/sources"
            and request.get_method() == "GET"
        ):
            return io.BytesIO(
                json.dumps(
                    [
                        {
                            "id": SOURCE_ID,
                            "name": "Library",
                            "type": "microsoft365",
                            "sync_status": "failed",
                            "configuration": {},
                        }
                    ]
                ).encode()
            )
        if path == f"/api/organisations/{ORG_ID}/sources/{SOURCE_ID}/sync":
            requests.append(request.get_method())
            return io.BytesIO(b'{"sync_status":"queued"}')
        return backend_response(request)

    app = AppTest.from_file(str(APP))
    app.session_state["metis_token"] = "signed-token"
    with patch("api_client.urlopen", side_effect=open_request):
        app.run()
        app.sidebar.radio[0].set_value("Knowledge")
        app.run()
        assert any("Last synchronization failed" in item.value for item in app.caption)
        next(button for button in app.button if button.label == "Sync now").click()
        app.run()
    assert not app.exception and requests == ["POST"]
    assert any("synchronization queued" in item.value for item in app.success)


def test_chat_selects_ready_document_and_explains_missing_sources():
    ready = document_response("indexed")
    ready[0]["title"] = "Payslip"
    app = authenticated_app("Chat", documents=ready)
    app.selectbox(key=f"chat_document_{ORG_ID}").select("Payslip")
    app.chat_input[0].set_value("How much did I get paid in 2 weeks?")
    payloads = []

    def open_request(request, timeout):
        if request.get_method() == "POST" and request.full_url.endswith("/chat"):
            payloads.append(json.loads(request.data))
        return backend_response(request, documents=ready)

    with patch("api_client.urlopen", side_effect=open_request):
        app.run()
    assert not app.exception
    assert payloads[0]["document_id"] == DOC_ID
    assert any(
        "No supporting document sources were returned" in item.value
        for item in app.caption
    )


def test_chat_multi_selection_sends_shared_scope():
    ready = document_response("indexed")
    app = authenticated_app("Chat", documents=ready)
    app.multiselect(key=f"chat_sources_{ORG_ID}").select(SOURCE_ID)
    app.multiselect(key=f"chat_documents_{ORG_ID}").select(DOC_ID)
    app.chat_input[0].set_value("Compare this knowledge")
    payloads = []

    def open_request(request, timeout):
        if request.get_method() == "POST" and request.full_url.endswith("/chat"):
            payloads.append(json.loads(request.data))
        return backend_response(request, documents=ready)

    with patch("api_client.urlopen", side_effect=open_request):
        app.run()
    assert not app.exception
    assert payloads[0]["source_ids"] == [SOURCE_ID]
    assert payloads[0]["document_ids"] == [DOC_ID]
    assert "document_id" not in payloads[0]


def test_failed_chat_preserves_question_and_retry_request_id():
    app = authenticated_app("Chat")
    app.chat_input[0].set_value("What does the policy require?")
    payloads = []

    def failing(request, timeout):
        if request.get_method() == "POST" and request.full_url.endswith("/chat"):
            payloads.append(json.loads(request.data))
            raise HTTPError(
                request.full_url,
                503,
                "Unavailable",
                {},
                io.BytesIO(b'{"detail":"Chat provider is unavailable"}'),
            )
        return backend_response(request)

    with patch("api_client.urlopen", side_effect=failing):
        app.run()
    assert not app.exception
    assert (
        app.text_area(key="metis_retry_text").value == "What does the policy require?"
    )
    next(button for button in app.button if button.label == "Retry question").click()

    def working(request, timeout):
        if request.get_method() == "POST" and request.full_url.endswith("/chat"):
            payloads.append(json.loads(request.data))
        return backend_response(request)

    with patch("api_client.urlopen", side_effect=working):
        app.run()
    assert not app.exception
    assert payloads[0] == payloads[1]
    assert len(app.chat_message) == 2


def test_reopen_restores_scope_and_allows_feedback_rename_and_delete():
    app = AppTest.from_file(str(APP))
    app.session_state["metis_token"] = "signed-token"
    app.session_state["active_organisation"] = ORG_ID
    saved = {
        "id": "saved-1",
        "title": "January pay",
        "scope": {"source_ids": [SOURCE_ID], "document_ids": [DOC_ID]},
    }
    actions = []
    deleted = False

    def open_request(request, timeout):
        nonlocal deleted
        path = urlparse(request.full_url).path
        method = request.get_method()
        if path.endswith("/conversations") and method == "GET":
            return io.BytesIO(json.dumps([] if deleted else [saved]).encode())
        if path.endswith("/conversations/saved-1"):
            if method == "GET":
                return io.BytesIO(
                    json.dumps(
                        {
                            **saved,
                            "messages": [
                                {
                                    "id": "answer-1",
                                    "role": "assistant",
                                    "content": "Synthetic answer",
                                    "citations": [],
                                    "outcome": "partially_answered",
                                }
                            ],
                        }
                    ).encode()
                )
            actions.append((method, json.loads(request.data) if request.data else None))
            if method == "PATCH":
                saved["title"] = json.loads(request.data)["title"]
            if method == "DELETE":
                deleted = True
            return io.BytesIO(b"{}")
        if path.endswith("/feedback"):
            actions.append((method, json.loads(request.data)))
            return io.BytesIO(b"{}")
        return backend_response(request, documents=document_response("indexed"))

    with patch("api_client.urlopen", side_effect=open_request):
        app.run()
        app.sidebar.radio[0].set_value("Chat")
        app.run()
        app.selectbox(key=f"saved_conversation_{ORG_ID}").set_value("saved-1")
        app.run()
        next(
            button for button in app.button if button.label == "Open conversation"
        ).click()
        app.run()
        assert app.multiselect(key=f"chat_sources_{ORG_ID}").value == [SOURCE_ID]
        assert app.multiselect(key=f"chat_documents_{ORG_ID}").value == [DOC_ID]
        next(
            item for item in app.selectbox if item.label == "Was this helpful?"
        ).set_value("problem")
        next(item for item in app.selectbox if item.label == "Reason").set_value(
            "missing_information"
        )
        next(button for button in app.button if button.label == "Send feedback").click()
        app.run()
        next(
            item for item in app.text_input if item.label == "Conversation name"
        ).set_value("Payroll")
        next(
            button for button in app.button if button.label == "Rename conversation"
        ).click()
        app.run()
        next(
            button for button in app.button if button.label == "Delete conversation"
        ).click()
        app.run()
    assert not app.exception
    assert app.session_state["metis_chat_id"] is None
    assert actions[0][1]["vote"] == "problem"
    assert actions[1] == ("PATCH", {"title": "Payroll"})
    assert actions[2] == ("DELETE", None)
