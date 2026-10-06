from fastapi.testclient import TestClient

from backend import semantic_runtime


class Vector:
    def __len__(self):
        return 384

    def tolist(self):
        return [0.1] * 384


class Model:
    def embed(self, texts):
        return iter(Vector() for _ in texts)


def test_pinned_runtime_protocol_and_readiness(monkeypatch):
    monkeypatch.setattr(semantic_runtime, "model", Model)
    with TestClient(semantic_runtime.app) as client:
        assert client.get("/health").json()["dimensions"] == 384
        response = client.post(
            "/v1/embeddings",
            json={"model": semantic_runtime.MODEL_ID, "input": ["synthetic"]},
        )
        assert len(response.json()["data"][0]["embedding"]) == 384
        assert (
            client.post(
                "/v1/embeddings", json={"model": "wrong", "input": ["synthetic"]}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/embeddings",
                json={"model": semantic_runtime.MODEL_ID, "input": [""]},
            ).status_code
            == 422
        )

        def unavailable():
            raise RuntimeError("unavailable")

        monkeypatch.setattr(semantic_runtime, "model", unavailable)
        assert client.get("/health").status_code == 503
        assert (
            client.post(
                "/v1/embeddings",
                json={"model": semantic_runtime.MODEL_ID, "input": ["synthetic"]},
            ).status_code
            == 503
        )
