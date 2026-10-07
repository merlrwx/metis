import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
from backend.connectors import microsoft365
from backend.connectors.base import ConnectorError, ExpiredCheckpoint, RemoteDocument
from backend.connectors.microsoft365 import Microsoft365Source, NoRedirect


@pytest.fixture
def graph_source(monkeypatch):
    source_id = uuid4()
    prefix = f"METIS_M365_{source_id.hex.upper()}_"
    settings = {
        "TENANT_ID": str(uuid4()),
        "CLIENT_ID": str(uuid4()),
        "CLIENT_SECRET": "synthetic-client-secret",
        "DRIVE_ID": "b!test-drive",
        "ORGANISATION_LIBRARY": "true",
    }
    for key, value in settings.items():
        monkeypatch.setenv(prefix + key, value)
    return Microsoft365Source(source_id)


def json_response(value):
    return BytesIO(json.dumps(value).encode())


@pytest.mark.asyncio
async def test_oauth_delta_pagination_and_download_do_not_forward_bearer_token(
    graph_source,
):
    requests = []
    next_link = graph_source.drive_url + "/root/delta?page=2"
    delta_link = graph_source.drive_url + "/root/delta?token=synthetic"

    class Transport:
        def open(self, request, timeout):
            requests.append(request)
            url = request.full_url
            if "login.microsoftonline.com" in url:
                body = parse_qs(request.data.decode())
                assert body["client_secret"] == ["synthetic-client-secret"]
                assert body["grant_type"] == ["client_credentials"]
                return json_response({"access_token": "synthetic-access-token"})
            if url.endswith("/content"):
                assert (
                    request.get_header("Authorization")
                    == "Bearer synthetic-access-token"
                )
                raise HTTPError(
                    url,
                    302,
                    "Found",
                    {
                        "Location": "https://example.sharepoint.com/download?token=synthetic"
                    },
                    None,
                )
            if url.startswith("https://example.sharepoint.com/download"):
                assert request.get_header("Authorization") is None
                return BytesIO(b"Library policy")
            assert (
                request.get_header("Authorization") == "Bearer synthetic-access-token"
            )
            if url == next_link:
                return json_response(
                    {
                        "value": [{"id": "removed", "deleted": {}}],
                        "@odata.deltaLink": delta_link,
                    }
                )
            return json_response(
                {
                    "value": [
                        {
                            "id": "file-1",
                            "name": "policy.txt",
                            "file": {"mimeType": "text/plain"},
                            "eTag": "v1",
                            "lastModifiedDateTime": "2026-10-06T00:00:00Z",
                            "webUrl": "https://example.sharepoint.com/policy.txt",
                        },
                        {"id": "folder", "folder": {}},
                    ],
                    "@odata.nextLink": next_link,
                }
            )

    graph_source.opener = Transport()
    changes = await graph_source.list_documents()
    assert changes.full_snapshot is True and changes.checkpoint == delta_link
    assert len(changes.documents) == 2 and changes.documents[1].deleted is True
    assert await graph_source.fetch_document(changes.documents[0]) == b"Library policy"
    assert (
        len(
            [
                request
                for request in requests
                if "login.microsoftonline.com" in request.full_url
            ]
        )
        == 1
    )
    assert (
        NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test")
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/data",
        "http://graph.microsoft.com/v1.0/drives/b!test-drive/root/delta",
        "https://graph.microsoft.com/v1.0/drives/foreign-drive/root/delta",
        "https://graph.microsoft.com/v1.0/drives/b!test-drive/../foreign-drive/root/delta",
    ],
)
async def test_untrusted_checkpoint_cannot_send_credentials(graph_source, url):
    class Transport:
        def open(self, request, timeout):
            raise AssertionError("Network must not be contacted")

    graph_source.opener = Transport()
    with pytest.raises(ConnectorError):
        await graph_source.list_documents(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/private",
        "http://example.sharepoint.com/private",
        "https://example.sharepoint.com.evil.test/private",
        "https://user@example.sharepoint.com/private",
    ],
)
async def test_download_redirect_rejects_untrusted_destinations(graph_source, url):
    graph_source.token = "synthetic-access-token"

    class Transport:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 302, "Found", {"Location": url}, None)

    graph_source.opener = Transport()
    with pytest.raises(ConnectorError):
        await graph_source.fetch_document(
            RemoteDocument("file", "", "", "", None, None)
        )


@pytest.mark.asyncio
async def test_download_size_and_expired_checkpoint_are_bounded(
    graph_source, monkeypatch
):
    graph_source.token = "synthetic-access-token"
    monkeypatch.setattr(microsoft365, "MAX_UPLOAD_BYTES", 5)

    class TooLarge:
        def open(self, request, timeout):
            return BytesIO(b"123456")

    graph_source.opener = TooLarge()
    with pytest.raises(ConnectorError):
        await graph_source.fetch_document(
            RemoteDocument("file", "", "", "", None, None)
        )

    class Expired:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 410, "Gone", {}, None)

    graph_source.opener = Expired()
    with pytest.raises(ExpiredCheckpoint):
        await graph_source.list_documents(
            graph_source.drive_url + "/root/delta?token=expired"
        )


def test_credentials_and_library_approval_are_bound_to_source_id(graph_source):
    with pytest.raises(ConnectorError):
        Microsoft365Source(uuid4())


@pytest.mark.asyncio
async def test_connection_check_authenticates_only_configured_library_root(
    graph_source,
):
    requested = []

    class Transport:
        def open(self, request, timeout):
            requested.append(request.full_url)
            if "login.microsoftonline.com" in request.full_url:
                return json_response({"access_token": "synthetic-access-token"})
            assert request.full_url == graph_source.drive_url + "/root"
            assert (
                request.get_header("Authorization") == "Bearer synthetic-access-token"
            )
            return json_response(
                {
                    "name": "Approved policies",
                    "folder": {},
                    "webUrl": "https://example.sharepoint.com/?token=private",
                }
            )

    graph_source.opener = Transport()
    result = await graph_source.check_connection()
    assert result == {"library_name": "Approved policies"}
    assert len(requested) == 2
