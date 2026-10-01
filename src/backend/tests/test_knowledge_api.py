from backend.main import app
from backend_test_client import authenticated_client


def test_organisation_source_and_document_metadata_persist():
    with authenticated_client(app) as client:
        organisation = client.post("/api/organisations", json={"name": "North Clinic"})
        assert organisation.status_code == 201
        organisation_id = organisation.json()["id"]

        source = client.post(
            f"/api/organisations/{organisation_id}/sources",
            json={"name": "Policies", "type": "upload"},
        )
        assert source.status_code == 201
        source_id = source.json()["id"]

        created = client.post(
            f"/api/organisations/{organisation_id}/documents",
            json={
                "title": "Medication policy",
                "source_id": source_id,
                "source_uri": "upload://medication-policy",
            },
        )
        assert created.status_code == 201
        document_id = created.json()["id"]

        documents = client.get(f"/api/organisations/{organisation_id}/documents")

    assert [document["id"] for document in documents.json()] == [document_id]
    assert documents.json()[0]["title"] == "Medication policy"


def test_document_access_is_organisation_scoped():
    with authenticated_client(app) as client:
        first = client.post("/api/organisations", json={"name": "A"}).json()
        second = client.post("/api/organisations", json={"name": "B"}).json()
        source = client.post(
            f"/api/organisations/{first['id']}/sources", json={"name": "A files"}
        ).json()

        denied = client.post(
            f"/api/organisations/{second['id']}/documents",
            json={"title": "A secret", "source_id": source["id"]},
        )
        missing = client.get(
            "/api/organisations/00000000-0000-0000-0000-000000000000/documents"
        )

    assert denied.status_code == 404
    assert missing.status_code == 404


def test_missing_organisation_and_source_are_not_created():
    with authenticated_client(app) as client:
        response = client.post(
            "/api/organisations/00000000-0000-0000-0000-000000000000/sources",
            json={"name": "Unknown"},
        )
    assert response.status_code == 404
