"""Combine measured evaluation artifacts into a versioned, comparable report."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from backend.services.answer_prompts import PROMPT_VERSION

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "src/backend/tests/fixtures"
DEFAULT_ARTIFACTS = {
    "retrieval": Path(".agent/semantic-evaluation.json"),
    "answers": Path(".agent/answer-evaluation.json"),
    "pipeline": Path(".agent/pipeline-evaluation.json"),
}


def read_json(path):
    return json.loads(path.read_text())


def summarize(path):
    if not path.exists():
        return None
    artifact = read_json(path)
    rows = artifact.get("cases", [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    categories = {}
    for row in rows:
        category = row.get("failure_kind") or row.get("failure_stage")
        if category is None and row.get("passed") is False:
            category = "unclassified_failure"
        if category:
            categories[category] = categories.get(category, 0) + 1
    measured = [row for row in rows if "passed" in row or "rank" in row]
    passed = sum(
        row.get("passed", row.get("rank") is not None) is True for row in measured
    )
    return {
        "artifact": str(path),
        "cases": len(rows),
        "measured_cases": len(measured),
        "passed_cases": passed,
        "failure_categories": dict(sorted(categories.items())),
        "configuration": {
            key: artifact.get(key)
            for key in (
                "embedding_provider",
                "embedding_model",
                "chat_provider",
                "chat_model",
                "retrieval_config",
                "prompt_version",
            )
            if key in artifact
        },
    }


def fixture_manifest():
    names = ("heldout_semantic_retrieval.json", "heldout_answer_quality.json")
    return {
        name: {
            "sha256": hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest(),
            "cases": len(read_json(FIXTURES / name)),
        }
        for name in names
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in DEFAULT_ARTIFACTS:
        parser.add_argument(f"--{name}", type=Path, help=f"{name} evaluation artifact")
    parser.add_argument("--previous", type=Path, help="prior quality report to compare")
    parser.add_argument(
        "--output", type=Path, default=Path(".agent/quality-report.json")
    )
    args = parser.parse_args()
    artifacts = {}
    for name, default in DEFAULT_ARTIFACTS.items():
        result = summarize(getattr(args, name) or default)
        if result:
            artifacts[name] = result
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "prompt_version": PROMPT_VERSION,
        "configuration": {
            "embedding_provider": os.getenv(
                "EMBEDDING_PROVIDER", "configured at runtime"
            ),
            "embedding_model": os.getenv("EMBEDDING_MODEL", "configured at runtime"),
            "chat_model": os.getenv("GPTMOCK_MODEL", "configured at runtime"),
            "retrieval_config": {
                "candidate_count": os.getenv("RETRIEVAL_CANDIDATE_COUNT", "default"),
                "evidence_count": os.getenv("RETRIEVAL_EVIDENCE_COUNT", "default"),
                "minimum_relevance": os.getenv("RETRIEVAL_MIN_RELEVANCE", "default"),
                "evidence_token_budget": os.getenv(
                    "RAG_EVIDENCE_TOKEN_BUDGET", "default"
                ),
            },
        },
        "heldout_fixtures": fixture_manifest(),
        "evaluations": artifacts,
        "quality_claim": "No target is asserted; only supplied measured artifacts are summarized.",
    }
    if args.previous:
        previous = read_json(args.previous)
        comparisons = {}
        for name, current in artifacts.items():
            old = previous.get("evaluations", {}).get(name)
            if old:
                names = set(old.get("failure_categories", {})) | set(
                    current["failure_categories"]
                )
                comparisons[name] = {
                    "prior_artifact": old.get("artifact"),
                    "case_count_delta": current["cases"] - old.get("cases", 0),
                    "passed_cases_delta": current["passed_cases"]
                    - old.get("passed_cases", 0),
                    "failure_category_deltas": {
                        key: current["failure_categories"].get(key, 0)
                        - old.get("failure_categories", {}).get(key, 0)
                        for key in sorted(names)
                    },
                }
        report["comparison"] = comparisons
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
