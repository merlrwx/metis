"""Compare embedding models and optional reranking against the synthetic QA corpus."""

import json
import math
import os
import resource
from pathlib import Path
from statistics import median
from time import perf_counter

from backend.embeddings import OpenAICompatibleEmbeddingProvider
from backend.reranking import configured_reranker

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "src/backend/tests/fixtures/semantic_retrieval.json"
)
DEFAULT_MODELS = [
    "sentence-transformers/all-MiniLM-L6-v2@d13954661f83248295ba75c1ed411eef3b7b936e",
    "BAAI/bge-small-en-v1.5",
    "intfloat/e5-small-v2",
]


def rank_case(provider, cases, case):
    query = case["question"]
    started = perf_counter()
    query_vector = provider.embed_query(query)
    similarities = []
    for candidate, vector in cases:
        denominator = math.sqrt(
            sum(value * value for value in query_vector)
        ) * math.sqrt(sum(value * value for value in vector))
        score = sum(a * b for a, b in zip(query_vector, vector)) / denominator
        similarities.append((candidate, score))
    similarities.sort(key=lambda row: row[1], reverse=True)
    return [row[0] for row in similarities], (perf_counter() - started) * 1000


def category(case):
    return "paraphrase" if "paraphrase" in case["id"] else "direct"


def metrics(rows):
    count = len(rows)
    return {
        "cases": count,
        "evidence_recall_at_5": round(
            sum(row["rank"] is not None and row["rank"] <= 5 for row in rows) / count, 4
        ),
        "evidence_recall_at_10": round(
            sum(row["rank"] is not None and row["rank"] <= 10 for row in rows) / count,
            4,
        ),
        "answer_quality_proxy_mrr": round(
            sum(1 / row["rank"] if row["rank"] else 0 for row in rows) / count, 4
        ),
        "evidence_precision_at_5": round(
            sum(row["rank"] is not None and row["rank"] <= 5 for row in rows)
            / (5 * count),
            4,
        ),
        "median_query_ms": round(median(row["latency_ms"] for row in rows), 2),
    }


def main():
    base_url = os.environ.get("EMBEDDING_BASE_URL", "http://localhost:8003/v1")
    api_key = os.environ.get("EMBEDDING_API_KEY", "metis-local")
    dimensions = int(os.environ.get("EMBEDDING_DIMENSIONS", "384"))
    model_names = json.loads(
        os.environ.get("METIS_BENCHMARK_MODELS", json.dumps(DEFAULT_MODELS))
    )
    cases = json.loads(
        Path(os.environ.get("METIS_RETRIEVAL_FIXTURE", str(FIXTURE))).read_text()
    )
    # One synthetic passage per annotated question; every query has a known supporting passage.
    report = {
        "fixture": str(FIXTURE),
        "method": "exact cosine over annotated synthetic passages; one query per fixture case; evidence labels from supporting_chunks",
        "models": [],
    }
    for model in model_names:
        row = {"model": model, "status": "ok"}
        try:
            provider = OpenAICompatibleEmbeddingProvider(
                base_url, api_key, model, dimensions=dimensions
            )
            start = perf_counter()
            vectors = provider.embed_documents([case["text"] for case in cases])
            row["corpus_embedding_ms"] = round((perf_counter() - start) * 1000, 2)
            corpus = list(zip(cases, vectors))
            baseline, reranked = [], []
            reranker = configured_reranker()
            for case in cases:
                ranked, elapsed = rank_case(provider, corpus, case)
                expected = set(case["supporting_chunks"])

                def find_rank(candidates, expected_chunks):
                    return next(
                        (
                            index
                            for index, item in enumerate(candidates, 1)
                            if any(
                                chunk in expected_chunks
                                for chunk in item.get("supporting_chunks", [])
                            )
                        ),
                        None,
                    )

                baseline.append(
                    {
                        "category": category(case),
                        "rank": find_rank(ranked, expected),
                        "latency_ms": elapsed,
                    }
                )
                if reranker:
                    # The API contract caps this benchmark stage at the top 40 candidates.
                    from types import SimpleNamespace

                    hits = [
                        SimpleNamespace(chunk=SimpleNamespace(content=item["text"]))
                        for item in ranked[:40]
                    ]
                    rerank_started = perf_counter()
                    order = reranker.rerank(case["question"], hits)
                    rerank_elapsed = (perf_counter() - rerank_started) * 1000
                    reordered = [ranked[hits.index(hit)] for hit in order]
                    tail = [item for item in ranked[40:]]
                    reranked.append(
                        {
                            "category": category(case),
                            "rank": find_rank(reordered + tail, expected),
                            "latency_ms": elapsed + rerank_elapsed,
                        }
                    )
            row["baseline"] = metrics(baseline)
            row["categories"] = {
                name: metrics([item for item in baseline if item["category"] == name])
                for name in sorted({item["category"] for item in baseline})
            }
            if reranker:
                row["reranked"] = metrics(reranked)
                row["reranked_categories"] = {
                    name: metrics(
                        [item for item in reranked if item["category"] == name]
                    )
                    for name in sorted({item["category"] for item in reranked})
                }
                row["reranker"] = {
                    "model": os.environ["METIS_RERANKER_MODEL"],
                    "top_n": min(40, len(cases)),
                }
            row["peak_rss_mb"] = round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1
            )
        except (
            ImportError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            row.update(status="unavailable", error=f"{type(error).__name__}: {error}")
        report["models"].append(row)
    output = Path(
        os.environ.get("METIS_BENCHMARK_OUTPUT", ".agent/embedding-benchmark.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if all(row["status"] != "ok" for row in report["models"]):
        raise SystemExit("No model benchmark completed; see exact errors in the report")


if __name__ == "__main__":
    main()
