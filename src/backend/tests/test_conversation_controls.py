from uuid import uuid4

import pytest
from backend.main import app
from backend.models import ChatRequestRecord, Message, MessageFeedback
from backend_test_client import authenticated_client, login_as, register_and_login
from sqlalchemy import func, select

from backend import database


@pytest.fixture(autouse=True)
def empty_retrieval(monkeypatch):
    monkeypatch.setattr(
        "backend.main.knowledge.search_chunks", lambda *args, **kwargs: []
    )


def test_retries_scopes_controls_and_feedback_are_persisted():
    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Conversations"}).json()[
            "id"
        ]
        source = client.post(
            f"/api/organisations/{org}/sources", json={"name": "Operations"}
        ).json()["id"]
        path = f"/api/organisations/{org}"
        payload = {
            "message": "What is the policy?",
            "source_ids": [source],
            "request_id": str(uuid4()),
        }
        first = client.post(path + "/chat", json=payload)
        assert first.status_code == 200
        response = first.json()
        assert client.post(path + "/chat", json=payload).json() == response
        assert (
            client.post(
                path + "/chat", json={**payload, "message": "A different question"}
            ).status_code
            == 409
        )
        with database.SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(Message)) == 2
            assert (
                session.scalar(select(func.count()).select_from(ChatRequestRecord)) == 1
            )
        conversation = path + "/conversations/" + response["conversation_id"]
        reopened = client.get(conversation).json()
        assert reopened["scope"] == {"source_ids": [source]}
        assert reopened["messages"][1]["outcome"] == "insufficient_evidence"
        followup = client.post(
            path + "/chat",
            json={
                "message": "What about last month?",
                "conversation_id": response["conversation_id"],
            },
        )
        assert followup.status_code == 200
        assert client.get(conversation).json()["scope"] == {"source_ids": [source]}
        assert (
            client.patch(conversation, json={"title": "Policies"}).json()["title"]
            == "Policies"
        )
        assert len(client.get(path + "/conversations?limit=1&offset=0").json()) == 1
        assert client.get(path + "/conversations?limit=1&offset=1").json() == []
        feedback = conversation + f"/messages/{response['message_id']}/feedback"
        assert (
            client.put(
                feedback,
                json={
                    "vote": "problem",
                    "reason": "missing_information",
                    "comment": "Synthetic feedback",
                },
            ).status_code
            == 204
        )
        assert client.put(feedback, json={"vote": "helpful"}).status_code == 204
        with database.SessionLocal() as session:
            assert session.scalar(select(MessageFeedback)).vote == "helpful"
        assert client.delete(conversation).status_code == 204
        assert client.get(conversation).status_code == 404
        assert client.get(path + "/conversations").json() == []


def test_member_cannot_list_rename_delete_or_feedback_on_someone_elses_conversation():
    with authenticated_client(app) as client:
        owner = client.get("/api/auth/me").json()["email"]
        org = client.post(
            "/api/organisations", json={"name": "Private conversations"}
        ).json()["id"]
        path = f"/api/organisations/{org}"
        response = client.post(
            path + "/chat", json={"message": "Policy question"}
        ).json()
        member = register_and_login(client)
        login_as(client, owner)
        client.post(
            path + "/members", json={"email": member["email"], "role": "member"}
        )
        login_as(client, member["email"])
        conversation = path + "/conversations/" + response["conversation_id"]
        assert client.get(path + "/conversations").json() == []
        assert client.get(conversation).status_code == 404
        assert client.patch(conversation, json={"title": "No"}).status_code == 404
        assert client.delete(conversation).status_code == 404
        assert (
            client.put(
                conversation + f"/messages/{response['message_id']}/feedback",
                json={"vote": "helpful"},
            ).status_code
            == 404
        )


def test_provider_failure_rolls_back_and_same_request_can_be_retried(monkeypatch):
    from backend import embeddings

    original = embeddings.get_embedding_provider
    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Retry failure"}).json()[
            "id"
        ]
        payload = {"message": "Question", "request_id": str(uuid4())}

        def unavailable():
            raise RuntimeError("Synthetic provider outage")

        monkeypatch.setattr(embeddings, "get_embedding_provider", unavailable)
        assert (
            client.post(f"/api/organisations/{org}/chat", json=payload).status_code
            == 503
        )
        with database.SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(Message)) == 0
            assert (
                session.scalar(select(func.count()).select_from(ChatRequestRecord)) == 0
            )
        monkeypatch.setattr(embeddings, "get_embedding_provider", original)
        assert (
            client.post(f"/api/organisations/{org}/chat", json=payload).status_code
            == 200
        )
        with database.SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(Message)) == 2


def test_stale_citations_are_redacted_from_history_transcript_and_cached_retry():
    from uuid import UUID

    from backend.services import conversations

    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Fresh evidence"}).json()[
            "id"
        ]
        payload = {"message": "Policy question", "request_id": str(uuid4())}
        response = client.post(f"/api/organisations/{org}/chat", json=payload).json()
        citation = {
            "chunk_id": str(uuid4()),
            "document_id": str(uuid4()),
            "document_title": "Deleted synthetic document",
            "source_id": None,
            "source_name": None,
            "page": None,
            "section": None,
            "snippet": "OLD_SYNTHETIC_SECRET",
        }
        with database.SessionLocal() as session:
            message = session.get(Message, UUID(response["message_id"]))
            message.content = "OLD_SYNTHETIC_SECRET"
            message.citations = [citation]
            record = session.scalar(select(ChatRequestRecord))
            record.response = {
                **record.response,
                "answer": "OLD_SYNTHETIC_SECRET",
                "citations": [citation],
            }
            session.commit()
            history = conversations.get_history(
                session, UUID(org), UUID(response["conversation_id"]), 20
            )
            assert history == [("user", "Policy question")]
        transcript = client.get(
            f"/api/organisations/{org}/conversations/{response['conversation_id']}"
        )
        assert "OLD_SYNTHETIC_SECRET" not in transcript.text
        retry = client.post(f"/api/organisations/{org}/chat", json=payload)
        assert "OLD_SYNTHETIC_SECRET" not in retry.text
        assert retry.json()["outcome"] == "insufficient_evidence"
