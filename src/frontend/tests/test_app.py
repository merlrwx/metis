import io
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


def test_grounded_chat_shows_backend_connection(monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "http://backend:8000/")
    with patch(
        "urllib.request.urlopen",
        return_value=io.BytesIO(b'{"name":"Metis","stage":"grounded-chat"}'),
    ) as request:
        app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert app.title[0].value == "Metis"
    assert app.success[0].value == "Metis API connected"
    request.assert_called_once_with("http://backend:8000/api/info", timeout=5)


@pytest.mark.parametrize("failure", [URLError("offline"), TimeoutError(), ValueError()])
def test_backend_unavailable_is_visible(failure):
    with patch("urllib.request.urlopen", side_effect=failure):
        app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert "Cannot connect" in app.error[0].value
    assert not app.success


@pytest.mark.parametrize("body", [b"invalid json", b'{"name":"Other"}'])
def test_invalid_backend_is_visible(body):
    with patch("urllib.request.urlopen", return_value=io.BytesIO(body)):
        app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert "Cannot connect" in app.error[0].value
