from uuid import uuid4

import pytest
from backend.main import app
from backend.schemas import ChatRequest, SearchRequest
from backend_test_client import authenticated_client


@pytest.mark.parametrize("route,field", [("search", "query"), ("chat", "message")])
@pytest.mark.parametrize("scope_field", ["source_ids", "document_ids", "source_id"])
def test_unavailable_scope_rejected_before_embedding(
    monkeypatch, route, field, scope_field
):
    def unexpected_provider():
        pytest.fail("An invalid scope must be rejected before calling a provider")

    monkeypatch.setattr(
        "backend.main.embeddings.get_embedding_provider", unexpected_provider
    )
    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Scope"}).json()["id"]
        identifier = str(uuid4())
        value = [identifier] if scope_field.endswith("ids") else identifier
        result = client.post(
            f"/api/organisations/{org}/{route}",
            json={field: "question", scope_field: value},
        )
        assert result.status_code == 404


@pytest.mark.parametrize(
    "schema,field", [(SearchRequest, "query"), (ChatRequest, "message")]
)
def test_scope_is_shared_and_empty_lists_are_invalid(schema, field):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        schema(**{field: "question", "source_ids": []})
    identifiers = [uuid4(), uuid4()]
    assert (
        schema(**{field: "question", "document_ids": identifiers}).document_ids
        == identifiers
    )


def test_conflicting_legacy_scope_rejected():
    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Scope"}).json()["id"]
        result = client.post(
            f"/api/organisations/{org}/chat",
            json={
                "message": "question",
                "document_id": str(uuid4()),
                "document_ids": [str(uuid4())],
            },
        )
        assert result.status_code == 422


def test_groups_crud_and_empty_scope_never_widens():
    from uuid import UUID

    from backend.main import request_scope

    from backend import database

    with authenticated_client(app) as client:
        org = client.post("/api/organisations", json={"name": "Groups"}).json()["id"]
        foreign = client.post("/api/organisations", json={"name": "Other"}).json()["id"]
        source = client.post(
            f"/api/organisations/{org}/sources",
            json={"name": "Payroll", "type": "upload"},
        ).json()["id"]
        path = f"/api/organisations/{org}/groups"
        created = client.post(path, json={"name": "Payroll", "source_ids": [source]})
        assert created.status_code == 201
        group = created.json()
        assert client.get(path).json() == [group]
        with database.SessionLocal() as session:
            assert request_scope(
                session,
                UUID(org),
                ChatRequest(message="question", group_id=group["id"]),
            ) == ([UUID(source)], None)
        assert (
            client.put(
                f"/api/organisations/{foreign}/groups/{group['id']}",
                json={"name": "Invalid", "source_ids": []},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/organisations/{foreign}/groups",
                json={"name": "Invalid", "source_ids": [source]},
            ).status_code
            == 404
        )
        assert (
            client.put(
                f"{path}/{group['id']}", json={"name": "Empty", "source_ids": []}
            ).status_code
            == 200
        )
        with database.SessionLocal() as session:
            assert request_scope(
                session,
                UUID(org),
                SearchRequest(query="question", group_id=group["id"]),
            ) == ([], None)
        assert client.delete(f"{path}/{group['id']}").status_code == 204
        assert client.get(path).json() == []


def test_evidence_diversity_overlap_and_budget():
    from types import SimpleNamespace

    from backend.services.knowledge import SearchHit, select_evidence

    first, second, version = uuid4(), uuid4(), uuid4()

    def hit(document, start, end, score):
        return SearchHit(
            SimpleNamespace(
                document_version_id=version if document == first else second,
                start_offset=start,
                end_offset=end,
                content="x" * (end - start),
            ),
            SimpleNamespace(id=document),
            None,
            score,
        )

    candidates = [
        hit(first, 0, 100, 0.9),
        hit(first, 50, 150, 0.8),
        hit(first, 150, 250, 0.7),
        hit(second, 0, 100, 0.6),
    ]
    selected = select_evidence(candidates, 3)
    assert [result.document.id for result in selected] == [first, second, first]
    assert candidates[1] not in selected
    assert len(select_evidence(candidates, 3, budget=150)) == 1


def test_members_can_read_groups_but_cannot_change_them():
    from backend_test_client import login_as, register_and_login

    with authenticated_client(app) as client:
        owner = client.get("/api/auth/me").json()["email"]
        org = client.post("/api/organisations", json={"name": "Permissions"}).json()[
            "id"
        ]
        path = f"/api/organisations/{org}/groups"
        group = client.post(path, json={"name": "Operations", "source_ids": []}).json()
        member = register_and_login(client)
        login_as(client, owner)
        assert (
            client.post(
                f"/api/organisations/{org}/members",
                json={"email": member["email"], "role": "member"},
            ).status_code
            == 201
        )
        login_as(client, member["email"])
        assert client.get(path).json() == [group]
        assert (
            client.post(path, json={"name": "No", "source_ids": []}).status_code == 403
        )
        assert (
            client.put(
                f"{path}/{group['id']}", json={"name": "No", "source_ids": []}
            ).status_code
            == 403
        )
        assert client.delete(f"{path}/{group['id']}").status_code == 403
