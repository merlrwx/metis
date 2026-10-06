import io
import json
import math

import pytest

from backend import embeddings


def cosine_similarity(left, right):
    return sum(a * b for a, b in zip(left, right))


def test_hashing_provider_is_deterministic_and_ranks_shared_terms():
    provider = embeddings.HashingEmbeddingProvider()
    query = provider.embed_query("What do we do after a medication incident?")
    relevant = provider.embed_query(
        "After a medication incident, notify the supervisor and record the response."
    )
    unrelated = provider.embed_query("Parking permits renew each January.")

    assert len(query) == embeddings.EMBEDDING_DIMENSIONS
    assert query == provider.embed_query("What do we do after a medication incident?")
    assert math.isclose(sum(value * value for value in query), 1.0, rel_tol=1e-9)
    assert cosine_similarity(query, relevant) > cosine_similarity(query, unrelated)


def test_openai_compatible_provider_sorts_responses_and_checks_dimensions(monkeypatch):
    vectors = [[float(index)] * embeddings.EMBEDDING_DIMENSIONS for index in (1, 0)]

    def fake_urlopen(request, timeout):
        assert request.full_url == "http://embedding.test/v1/embeddings"
        assert request.get_header("Authorization") == "Bearer test-key"
        assert timeout == 5
        assert json.loads(request.data) == {
            "model": "fixture-model",
            "input": ["first", "second"],
        }
        return io.BytesIO(
            json.dumps(
                {
                    "data": [
                        {"index": 1, "embedding": vectors[0]},
                        {"index": 0, "embedding": vectors[1]},
                    ]
                }
            ).encode()
        )

    monkeypatch.setattr(embeddings, "urlopen", fake_urlopen)
    provider = embeddings.OpenAICompatibleEmbeddingProvider(
        "http://embedding.test/v1", "test-key", "fixture-model", timeout=5
    )
    result = provider.embed_documents(["first", "second"])

    assert result[0][0] == 0
    assert result[1][0] == 1


def test_openai_compatible_provider_rejects_wrong_vector_dimensions(monkeypatch):
    response = io.BytesIO(
        json.dumps({"data": [{"index": 0, "embedding": [0.1, 0.2]}]}).encode()
    )
    monkeypatch.setattr(embeddings, "urlopen", lambda *args, **kwargs: response)
    provider = embeddings.OpenAICompatibleEmbeddingProvider(
        "http://embedding.test/v1", "test-key", "fixture-model"
    )

    with pytest.raises(RuntimeError, match="1536 finite values"):
        provider.embed_query("text")


def test_openai_compatible_provider_rejects_invalid_result_indexes(monkeypatch):
    response = io.BytesIO(
        json.dumps(
            {
                "data": [
                    {"index": 0, "embedding": [0.1] * embeddings.EMBEDDING_DIMENSIONS},
                    {"index": 0, "embedding": [0.2] * embeddings.EMBEDDING_DIMENSIONS},
                ]
            }
        ).encode()
    )
    monkeypatch.setattr(embeddings, "urlopen", lambda *args, **kwargs: response)
    provider = embeddings.OpenAICompatibleEmbeddingProvider(
        "http://embedding.test/v1", "test-key", "fixture-model"
    )

    with pytest.raises(RuntimeError, match="result indexes"):
        provider.embed_documents(["first", "second"])


def test_openai_compatible_provider_requires_an_api_key(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai-compatible")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="API_KEY"):
        embeddings.get_embedding_provider()


def test_semantic_provider_accepts_configured_dimensions_and_rejects_mismatch(
    monkeypatch,
):
    provider = embeddings.OpenAICompatibleEmbeddingProvider(
        "http://local/v1", "local", "pinned-model", dimensions=384
    )
    monkeypatch.setattr(
        embeddings,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(
            json.dumps({"data": [{"index": 0, "embedding": [0.1] * 384}]}).encode()
        ),
    )
    assert len(provider.embed_query("synthetic")) == 384
    assert provider.model_id.endswith(":dimensions=384")
    monkeypatch.setattr(
        embeddings,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(
            json.dumps({"data": [{"index": 0, "embedding": [0.1] * 1536}]}).encode()
        ),
    )
    with pytest.raises(RuntimeError, match="384 finite values"):
        provider.embed_query("synthetic")
