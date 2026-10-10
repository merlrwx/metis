import json
from pathlib import Path
from unittest.mock import patch

from backend.evaluation import failure_stage
from backend.evaluation_fixtures import build_synthetic_document
from backend.services import ingestion

from backend import observability


def test_failure_stages_distinguish_retrieval_filtering_and_answer_failures():
    assert (
        failure_stage(expected_retrieved=False, expected_supplied=False)
        == "evidence_not_retrieved"
    )
    assert (
        failure_stage(expected_retrieved=True, expected_supplied=False)
        == "evidence_retrieved_but_discarded"
    )
    assert (
        failure_stage(expected_retrieved=True, expected_supplied=True)
        == "evidence_supplied_answer_or_citation_incorrect"
    )


def test_diagnostics_are_opt_in(monkeypatch):
    monkeypatch.delenv("METIS_DIAGNOSTICS_ENABLED", raising=False)
    with patch.object(observability, "log_event") as log_event:
        observability.diagnostic_event("retrieval", score=0.8)
        log_event.assert_not_called()
    monkeypatch.setenv("METIS_DIAGNOSTICS_ENABLED", "true")
    with patch.object(observability, "log_event") as log_event:
        observability.diagnostic_event("retrieval", score=0.8)
        log_event.assert_called_once_with("diagnostic_retrieval", score=0.8)


def test_evaluation_fixtures_have_fifty_annotated_cases_across_formats():
    fixture_dir = Path(__file__).parent / "fixtures"
    for name in ("answer_quality.json", "semantic_retrieval.json"):
        cases = json.loads((fixture_dir / name).read_text())
        assert len(cases) >= 50
        assert {case["format"] for case in cases} >= {
            "txt",
            "markdown",
            "csv",
            "docx",
            "pdf",
        }
        assert all(
            case["source"]
            and case["source_version"]
            and case["expected_answer"]
            and case["supporting_sections"]
            and case["supporting_chunks"]
            for case in cases
        )


def test_format_builders_produce_files_readable_by_production_extractors():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "semantic_retrieval.json").read_text()
    )
    extensions = {
        "pdf": ".pdf",
        "docx": ".docx",
        "markdown": ".md",
        "txt": ".txt",
        "csv": ".csv",
    }
    for format_name, extension in extensions.items():
        case = next(case for case in fixture if case["format"] == format_name)
        filename, mime_type, contents = build_synthetic_document(
            case["text"], format_name, case["supporting_sections"][0]
        )
        assert filename.endswith(extension)
        assert ingestion.validate_upload(filename, mime_type, contents) == mime_type
        extracted = ingestion.extract_document(filename, mime_type, contents)
        assert extracted.text
        assert extracted.metadata.get("pages") or extracted.metadata.get("sections")
