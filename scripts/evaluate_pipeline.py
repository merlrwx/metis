"""Opt-in synthetic corpus: actual ingestion, scoped retrieval and live generation."""

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from backend.connectors.base import RemoteDocument
from backend.models import Organisation, Source
from backend.services import documents, followups, knowledge, rag, source_sync
from backend.storage import LocalObjectStorage
from backend.tasks import run_ingestion_job
from sqlalchemy import delete
from sqlalchemy.engine import make_url

from backend import chat, database, embeddings


async def evaluate():
    cases = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "src/backend/tests/fixtures/answer_quality.json"
        ).read_text()
    )
    pay_texts = cases[1]["texts"]
    cases.extend(
        [
            {
                "id": "payslip-sum",
                "question": "What is the total AUD net payment for January and February 2026?",
                "texts": pay_texts,
                "outcome": "answered",
                "claims": ["4100.35"],
                "citations": 2,
                "scoped": True,
            },
            {
                "id": "payslip-average",
                "question": "What is the average AUD net payment for January and February 2026?",
                "texts": pay_texts,
                "outcome": "answered",
                "claims": ["2050.175"],
                "citations": 2,
                "scoped": True,
            },
            {
                "id": "exact-invoice",
                "question": "What amount is recorded for INV-2047?",
                "texts": ["Invoice INV-2047: amount AUD 345.67, due 21 February 2026."],
                "outcome": "answered",
                "claims": ["345.67"],
                "citations": 1,
            },
        ]
    )
    embedding = embeddings.get_embedding_provider()
    provider = chat.get_chat_provider()
    tenants = [uuid4(), uuid4()]
    report = []
    with TemporaryDirectory() as folder, database.SessionLocal() as session:
        os.environ["OBJECT_STORAGE_BACKEND"] = "local"
        os.environ["OBJECT_STORAGE_LOCAL_DIR"] = folder
        storage = LocalObjectStorage(Path(folder))
        try:
            session.add_all(
                Organisation(id=id, name="Synthetic pipeline evaluation")
                for id in tenants
            )
            source = Source(
                id=uuid4(),
                organisation_id=tenants[0],
                type="microsoft365",
                name="Fixture library (not live Microsoft 365)",
                configuration={},
            )
            session.add(source)
            session.commit()
            # Deduplicate shared excerpts, and use consistent payroll values throughout.
            corpus = {}
            expected = {}
            for case in cases:
                expected[case["id"]] = set()
                for text in case["texts"]:
                    text = text.replace("2000.00", "2000.10").replace(
                        "2100.00", "2100.25"
                    )
                    if text not in corpus:
                        if "runbook" in text:
                            doc, job = source_sync.store_external_document(
                                session,
                                source,
                                RemoteDocument(
                                    "runbook",
                                    "runbook.txt",
                                    "text/plain",
                                    "synthetic-v1",
                                    None,
                                    "https://example.invalid/runbook",
                                ),
                                text.encode(),
                                storage,
                            )
                        else:
                            doc, job, _ = documents.upload_document(
                                session,
                                tenants[0],
                                f"synthetic-{len(corpus)}.txt",
                                "text/plain",
                                text.encode(),
                                storage,
                            )
                        corpus[text] = doc.id
                        await run_ingestion_job(str(job.id), str(tenants[0]))
                    expected[case["id"]].add(corpus[text])
            # Make the unscoped census exceed the bounded 20-chunk evidence proof.
            # Scoped payroll totals remain complete and must still succeed.
            for index in range(12):
                _, job, _ = documents.upload_document(
                    session,
                    tenants[0],
                    f"catalogue-{index}.txt",
                    "text/plain",
                    f"Historical pottery catalogue accession CER-{index}: blue glazed ceramic bowl, nineteenth-century sculpture collection.".encode(),
                    storage,
                )
                await run_ingestion_job(str(job.id), str(tenants[0]))
            foreign, job, _ = documents.upload_document(
                session,
                tenants[1],
                "foreign.txt",
                "text/plain",
                b"Incident policy: notify the supervisor immediately after an incident.",
                storage,
            )
            await run_ingestion_job(str(job.id), str(tenants[1]))
            for case in cases:
                selected = list(expected[case["id"]]) if case.get("scoped") else None
                started = perf_counter()
                query = followups.rewrite(case["question"], case.get("history", []))
                assert query is not None
                hits = knowledge.search_chunks(
                    session,
                    tenants[0],
                    embedding.embed_query(query),
                    embedding.model_id,
                    5,
                    query_text=query,
                    expand_neighbors=True,
                    document_ids=selected,
                )
                assert all(
                    hit.chunk.organisation_id == tenants[0]
                    and hit.document.id != foreign.id
                    for hit in hits
                )
                retrieved = {hit.document.id for hit in hits}
                retrieval_seconds = perf_counter() - started
                # Use the same bounded complete-scope path as chat; never substitute curated hits.
                complete_scope = False
                if rag.needs_complete_scope(case["question"]):
                    complete = knowledge.complete_scoped_evidence(
                        session, tenants[0], embedding.model_id, None, selected
                    )
                    if complete is not None:
                        hits, complete_scope = complete, True
                result = rag.answer_question(
                    case["question"],
                    case.get("history", []),
                    hits,
                    provider,
                    complete_scope=complete_scope,
                    document_ids=selected,
                )
                outcomes = (
                    case["outcome"]
                    if isinstance(case["outcome"], list)
                    else [case["outcome"]]
                )
                passed = (
                    result.outcome in outcomes
                    and len(result.citations) >= case["citations"]
                    and all(
                        claim.casefold() in result.content.casefold()
                        for claim in case["claims"]
                    )
                )
                # The production complete guard wording is code-owned, not model-generated.
                if case["id"] == "complete-guard":
                    passed = result.outcome == "clarification_needed"
                row = {
                    "case": case["id"],
                    "passed": passed,
                    "expected_document_coverage": len(retrieved & expected[case["id"]])
                    / len(expected[case["id"]]),
                    "retrieval_seconds": round(retrieval_seconds, 3),
                    "total_seconds": round(perf_counter() - started, 3),
                    "outcome": result.outcome,
                    "citations": len(result.citations),
                    "answer": result.content,
                }
                report.append(row)
                print(json.dumps(row), flush=True)
        finally:
            session.rollback()
            session.execute(delete(Organisation).where(Organisation.id.in_(tenants)))
            session.commit()
    output = Path(".agent/pipeline-evaluation.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "embedding_model": embedding.model_id,
                "chat_model": os.environ.get("GPTMOCK_MODEL", "gpt-5.6-luna"),
                "source": "Microsoft 365 fixture; live ChatMock",
                "cases": report,
            },
            indent=2,
        )
        + "\n"
    )
    if not all(row["passed"] for row in report):
        raise SystemExit(
            "Review failed pipeline cases above; synthetic answers require human support review too"
        )


def main():
    url = make_url(os.environ.get("DATABASE_URL", "sqlite://"))
    if (
        os.environ.get("METIS_LIVE_CHAT_EVAL_ENABLED") != "true"
        or not os.environ.get("GPTMOCK_BASE_URL")
        or url.get_backend_name() != "postgresql"
        or not (url.database or "").endswith(("_verify", "_evaluation"))
    ):
        raise SystemExit(
            "Opt in with METIS_LIVE_CHAT_EVAL_ENABLED=true, GPTMOCK_BASE_URL and an isolated PostgreSQL database ending _verify or _evaluation"
        )
    asyncio.run(evaluate())


if __name__ == "__main__":
    main()
