import io
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from api_client import ApiError, MetisApi


def test_login_uses_oauth_form_fields():
    with patch(
        "api_client.urlopen",
        return_value=io.BytesIO(b'{"access_token":"signed-token"}'),
    ) as urlopen:
        token = MetisApi("http://backend:8000").login("user@example.test", "secret")

    request = urlopen.call_args.args[0]
    assert token == "signed-token"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert request.data == b"username=user%40example.test&password=secret"


def test_upload_builds_safe_multipart_request():
    with patch("api_client.urlopen", return_value=io.BytesIO(b"{}")) as urlopen:
        MetisApi("http://backend:8000").request(
            "POST",
            "/upload",
            token="signed-token",
            upload=(
                "policy.txt\r\nX-Evil: yes",
                b"policy text",
                "text/plain\r\nX-Evil",
            ),
            source_id="source-id",
        )

    request = urlopen.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer signed-token"
    assert request.get_header("Content-type").startswith(
        "multipart/form-data; boundary="
    )
    assert b'filename="policy.txt__X-Evil_ yes"' in request.data
    assert b"Content-Type: application/octet-stream" in request.data
    assert b'name="source_id"\r\n\r\nsource-id' in request.data
    assert b"\r\nX-Evil:" not in request.data


def test_http_error_detail_is_preserved():
    error = HTTPError(
        "http://backend:8000/resource",
        403,
        "Forbidden",
        {},
        io.BytesIO(b'{"detail":"Not allowed"}'),
    )
    with (
        patch("api_client.urlopen", side_effect=error),
        pytest.raises(ApiError) as raised,
    ):
        MetisApi("http://backend:8000").request("GET", "/resource")

    assert raised.value.status_code == 403
    assert raised.value.detail == "Not allowed"


def test_login_rejects_an_invalid_token_response():
    with (
        patch("api_client.urlopen", return_value=io.BytesIO(b"{}")),
        pytest.raises(ApiError, match="did not return a sign-in token"),
    ):
        MetisApi("http://backend:8000").login("user@example.test", "secret")


def test_original_bytes_are_downloaded_with_authorisation():
    with patch(
        "api_client.urlopen", return_value=io.BytesIO(b"%PDF synthetic")
    ) as urlopen:
        result = MetisApi("http://backend:8000").request(
            "GET", "/original", token="signed-token", raw=True
        )
    assert result == b"%PDF synthetic"
    assert (
        urlopen.call_args.args[0].get_header("Authorization") == "Bearer signed-token"
    )
