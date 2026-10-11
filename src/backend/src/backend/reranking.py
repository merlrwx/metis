"""Optional reranking providers. Nothing in the application enables one by default."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.services.knowledge import SearchHit


class JinaReranker:
    """Client for the Jina-compatible /rerank API, useful with local gateways too."""

    def __init__(
        self, base_url: str, model: str, api_key: str = "", timeout: float = 30
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        request = Request(
            f"{self.base_url}/rerank",
            data=json.dumps(
                {
                    "model": self.model,
                    "query": query,
                    "documents": [hit.chunk.content for hit in hits],
                    "top_n": len(hits),
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Reranker returned HTTP {error.code}") from error
        except URLError as error:
            raise RuntimeError("Reranker could not be reached") from error
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise TypeError("Reranker returned an invalid response")
        indexes = [row.get("index") for row in rows if isinstance(row, dict)]
        if (
            len(indexes) != len(rows)
            or any(
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(hits)
                for index in indexes
            )
            or len(set(indexes)) != len(indexes)
        ):
            raise RuntimeError("Reranker returned invalid result indexes")
        return [hits[index] for index in indexes]


def configured_reranker() -> JinaReranker | None:
    base_url = os.environ.get("METIS_RERANKER_BASE_URL")
    if not base_url:
        return None
    model = os.environ.get("METIS_RERANKER_MODEL")
    if not model:
        raise RuntimeError("METIS_RERANKER_MODEL is required when reranking is enabled")
    return JinaReranker(base_url, model, os.environ.get("METIS_RERANKER_API_KEY", ""))
