import io

import pytest
from backend.storage import LocalObjectStorage, S3ObjectStorage


def test_local_storage_round_trip_and_delete(tmp_path):
    storage = LocalObjectStorage(tmp_path)
    storage.put("org/document/version", b"contents", "text/plain")

    assert storage.get("org/document/version") == b"contents"
    storage.delete("org/document/version")
    assert not (tmp_path / "org/document/version").exists()


def test_local_storage_rejects_path_traversal(tmp_path):
    storage = LocalObjectStorage(tmp_path)

    with pytest.raises(ValueError, match="Invalid object key"):
        storage.put("../outside", b"contents", "text/plain")


def test_s3_storage_uses_bucket_scoped_client_operations():
    class FakeS3:
        def __init__(self):
            self.calls = []

        def put_object(self, **kwargs):
            self.calls.append(("put", kwargs))

        def get_object(self, **kwargs):
            self.calls.append(("get", kwargs))
            return {"Body": io.BytesIO(b"contents")}

        def delete_object(self, **kwargs):
            self.calls.append(("delete", kwargs))

    client = FakeS3()
    storage = S3ObjectStorage("metis-files", client=client)

    storage.put("org/document", b"contents", "text/plain")
    assert storage.get("org/document") == b"contents"
    storage.delete("org/document")

    assert [call[0] for call in client.calls] == ["put", "get", "delete"]
    assert all(call[1]["Bucket"] == "metis-files" for call in client.calls)
    assert client.calls[0][1]["ContentType"] == "text/plain"
