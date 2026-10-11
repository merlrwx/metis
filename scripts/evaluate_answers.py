"""Opt-in live answer-quality review using synthetic excerpts, independent of retrieval."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

from backend.chat import AnswerFormatError, ChatProviderError, get_chat_provider
from backend.evaluation import answer_matches_case, failure_stage, provider_details
from backend.services.knowledge import SearchHit
from backend.services.rag import answer_question

from backend import embeddings


def main():
    if os.environ.get("METIS_LIVE_CHAT_EVAL_ENABLED") != "true" or not os.environ.get(
        "GPTMOCK_BASE_URL"
    ):
        raise SystemExit(
            "Set METIS_LIVE_CHAT_EVAL_ENABLED=true and an explicit local GPTMOCK_BASE_URL"
        )
    cases = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "src/backend/tests/fixtures/answer_quality.json"
        ).read_text()
    )
    provider = get_chat_provider()
    failures = []
    embedding = embeddings.get_embedding_provider()
    for case in cases:
        hits = [
            SearchHit(
                SimpleNamespace(
                    id=f"{case['id']}-{index}", content=text, page=None, section=None
                ),
                SimpleNamespace(
                    id=f"doc-{index}",
                    title=f"Synthetic {case['id']} {index}",
                    source_id=None,
                ),
                None,
                0.9,
            )
            for index, text in enumerate(case["texts"])
        ]
        try:
            result = answer_question(
                case["question"], case.get("history", []), hits, provider
            )
            failure_kind = (
                "missing_evidence"
                if result.outcome == "insufficient_evidence"
                else "unsupported_answer"
                if result.outcome == "partially_answered"
                else None
            )
        except AnswerFormatError:
            failure_kind = "format"
            result = None
        except ChatProviderError:
            failure_kind = "provider"
            result = None
        passed = result is not None and answer_matches_case(case, result)
        if not passed and result is not None and failure_kind is None:
            failure_kind = "unsupported_answer"
        row = {
            "case": case["id"],
            "passed": passed,
            "failure_kind": failure_kind if not passed else None,
            "failure_stage": failure_stage(
                expected_retrieved=True, expected_supplied=True
            )
            if not passed
            else None,
            "extraction_availability": "synthetic_excerpts_supplied",
            "index_readiness": "not_required_for_answer_only_evaluation",
            "reindex_readiness": "not_required_for_answer_only_evaluation",
            "outcome": result.outcome if result else None,
            "answer": result.content if result else None,
            "citations": len(result.citations) if result else 0,
            **provider_details(embedding, provider),
        }
        print(json.dumps(row), flush=True)
        if not passed:
            failures.append(case["id"])
    if failures:
        raise SystemExit(f"Review failed synthetic cases: {failures}")


if __name__ == "__main__":
    main()
