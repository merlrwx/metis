"""Opt-in grounded-answer evaluation using synthetic, public test documents."""

import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from backend.chat import get_chat_provider
from backend.embeddings import HashingEmbeddingProvider
from backend.models import Chunk, Document
from backend.services.knowledge import SearchHit
from backend.services.rag import answer_question


def evaluate(provider):
    fixtures = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "src/backend/tests/fixtures/rag_evaluation.json"
        ).read_text()
    )
    required_facts = {
        "medication-incident": [("record", "document", "log"), ("supervisor",)],
        "equipment-repair": [("inspect", "check"), ("record", "document", "log")],
        "workstation-access": [("manager",)],
    }
    embedding = HashingEmbeddingProvider()
    results = []
    for case in fixtures:
        query = embedding.embed_query(case["question"])
        vector = embedding.embed_query(case["relevant"])
        score = sum(a * b for a, b in zip(query, vector))
        hit = SearchHit(
            Chunk(id=uuid4(), content=case["relevant"]),
            Document(id=uuid4(), title=case["id"]),
            None,
            score,
        )
        start = perf_counter()
        answer = answer_question(case["question"], [], [hit], provider)
        text = answer.content.casefold()
        results.append(
            {
                "case": case["id"],
                "cited": bool(answer.citations),
                "expected_facts": all(
                    any(term in text for term in alternatives)
                    for alternatives in required_facts[case["id"]]
                ),
                "latency_seconds": round(perf_counter() - start, 3),
                "model": answer.completion.model_id if answer.completion else None,
                "input_tokens": answer.completion.input_tokens
                if answer.completion
                else None,
                "output_tokens": answer.completion.output_tokens
                if answer.completion
                else None,
            }
        )
    return results


def main():
    if os.environ.get("METIS_LIVE_LLM_ENABLED") != "true":
        raise SystemExit(
            "Set METIS_LIVE_LLM_ENABLED=true to authorize this live evaluation"
        )
    results = evaluate(get_chat_provider())
    report = {
        "cases": results,
        "passed": sum(case["cited"] and case["expected_facts"] for case in results),
        "total": len(results),
    }
    output = Path(
        os.environ.get("METIS_EVALUATION_OUTPUT", ".agent/llm-evaluation.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if report["passed"] != report["total"]:
        raise SystemExit("Grounded-answer evaluation failed; inspect case results")


if __name__ == "__main__":
    main()
