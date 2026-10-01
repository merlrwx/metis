import hashlib
import json
import math
import os
import re
from itertools import pairwise
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

EMBEDDING_DIMENSIONS = 1536
EMBEDDING_BATCH_SIZE = 64


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class InvalidEmbeddingInput(ValueError):
    pass


class HashingEmbeddingProvider:
    """Deterministic lexical vectors for development and tests, not production use."""

    model_id = "metis:feature-hash-v1"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        tokens = re.findall(r"\w+", text.casefold())
        if not tokens:
            raise InvalidEmbeddingInput("Query must contain at least one word")

        vector = [0.0] * EMBEDDING_DIMENSIONS
        features = [(token, 1.0) for token in tokens]
        features.extend((f"{left} {right}", 0.5) for left, right in pairwise(tokens))
        for feature, weight in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight

        magnitude = math.sqrt(sum(value * value for value in vector))
        return [value / magnitude for value in vector]


class OpenAICompatibleEmbeddingProvider:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.model_id = f"openai-compatible:{model}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        embeddings = []
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[start : start + EMBEDDING_BATCH_SIZE]
            embeddings.extend(self._embed_batch(batch))
        return embeddings

    def embed_query(self, text: str) -> list[float]:
        return self._embed_batch([text])[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps({"model": self.model, "input": texts}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                try:
                    payload = json.load(response)
                except (ValueError, UnicodeDecodeError) as error:
                    raise RuntimeError(
                        "Embedding provider returned invalid JSON"
                    ) from error
        except HTTPError as error:
            raise RuntimeError(
                f"Embedding provider returned HTTP {error.code}"
            ) from error
        except URLError as error:
            raise RuntimeError("Embedding provider could not be reached") from error

        if not isinstance(payload, dict):
            raise TypeError("Embedding provider returned an invalid response")
        rows = payload.get("data")
        if (
            not isinstance(rows, list)
            or len(rows) != len(texts)
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise RuntimeError("Embedding provider returned an invalid response")
        indexes = [row.get("index") for row in rows]
        if any(
            not isinstance(index, int) or isinstance(index, bool) for index in indexes
        ) or sorted(indexes) != list(range(len(texts))):
            raise RuntimeError("Embedding provider returned invalid result indexes")
        rows.sort(key=lambda row: row["index"])
        vectors = []
        for row in rows:
            vector = row.get("embedding")
            if (
                not isinstance(vector, list)
                or len(vector) != EMBEDDING_DIMENSIONS
                or any(
                    not isinstance(value, int | float) or isinstance(value, bool)
                    for value in vector
                )
                or any(not math.isfinite(value) for value in vector)
            ):
                raise RuntimeError(
                    f"Embedding provider must return {EMBEDDING_DIMENSIONS} finite values"
                )
            vectors.append([float(value) for value in vector])
        return vectors


def get_embedding_provider() -> EmbeddingProvider:
    provider = os.environ.get("EMBEDDING_PROVIDER", "openai-compatible").lower()
    if provider == "hashing":
        return HashingEmbeddingProvider()
    if provider == "openai-compatible":
        api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get(
            "OPENAI_API_KEY"
        )
        if not api_key:
            raise RuntimeError(
                "EMBEDDING_API_KEY or OPENAI_API_KEY is required for embeddings"
            )
        return OpenAICompatibleEmbeddingProvider(
            base_url=os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1"),
            api_key=api_key,
            model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        )
    raise RuntimeError(f"Unsupported embedding provider: {provider}")
