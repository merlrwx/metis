"""Opt-in synthetic ingestion and semantic PostgreSQL retrieval evaluation."""

import asyncio
import json
import os
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from backend.models import Organisation
from backend.services import documents, followups, knowledge, rag
from backend.storage import LocalObjectStorage
from backend.tasks import run_ingestion_job
from sqlalchemy import delete

from backend import database, embeddings


async def evaluate():
    provider = embeddings.get_embedding_provider()
    fixture = Path(
        os.environ.get(
            "METIS_RETRIEVAL_FIXTURE",
            str(
                Path(__file__).resolve().parents[1]
                / "src/backend/tests/fixtures/semantic_retrieval.json"
            ),
        )
    )
    cases = json.loads(fixture.read_text())
    ids = [uuid4(), uuid4()]
    results = []
    with TemporaryDirectory() as folder, database.SessionLocal() as session:
        old_folder = os.environ.get("OBJECT_STORAGE_LOCAL_DIR")
        old_backend = os.environ.get("OBJECT_STORAGE_BACKEND")
        os.environ["OBJECT_STORAGE_LOCAL_DIR"], os.environ["OBJECT_STORAGE_BACKEND"] = (
            folder,
            "local",
        )
        try:
            for organisation_id in ids:
                session.add(
                    Organisation(
                        id=organisation_id, name="Synthetic semantic evaluation"
                    )
                )
            session.commit()
            storage = LocalObjectStorage(Path(folder))
            document_ids = {}
            for case in cases:
                document, job, _ = documents.upload_document(
                    session,
                    ids[0],
                    f"{case['id']}.txt",
                    "text/plain",
                    case["text"].encode(),
                    storage,
                )
                document_ids[case["id"]] = document.id
                await run_ingestion_job(str(job.id), str(ids[0]))
            foreign, job, _ = documents.upload_document(
                session,
                ids[1],
                "foreign.txt",
                "text/plain",
                cases[0]["text"].encode(),
                storage,
            )
            await run_ingestion_job(str(job.id), str(ids[1]))
            for case in cases:
                started = perf_counter()
                query_text = followups.rewrite(
                    case["question"], case.get("history", [])
                )
                assert query_text is not None, (
                    "Fixture follow-up has no resolvable user context"
                )
                query = provider.embed_query(query_text)
                hits = knowledge.search_chunks(
                    session,
                    ids[0],
                    query,
                    provider.model_id,
                    5,
                    query_text=query_text,
                )
                assert all(hit.chunk.organisation_id == ids[0] for hit in hits)
                assert all(hit.document.id != foreign.id for hit in hits)
                ranked = [hit.document.id for hit in hits]
                rank = (
                    ranked.index(document_ids[case["id"]]) + 1
                    if document_ids[case["id"]] in ranked
                    else None
                )
                results.append(
                    {
                        "case": case["id"],
                        "rank": rank,
                        "best_score": round(hits[0].score, 4) if hits else None,
                        "seconds": round(perf_counter() - started, 4),
                    }
                )
            negatives = []
            for question in [
                "Who won the Mars chess tournament in 2070?",
                "What is the capital of Finland?",
                "Give me the recipe for chocolate cake.",
                "What colour is my car?",
            ]:
                hits = knowledge.search_chunks(
                    session,
                    ids[0],
                    provider.embed_query(question),
                    provider.model_id,
                    5,
                    query_text=question,
                )
                assert not rag.supporting_evidence(hits), (
                    "Unanswerable fixture incorrectly passes the relevance gate"
                )
                negatives.append(
                    {
                        "question": question,
                        "best_score": round(hits[0].score, 4) if hits else None,
                    }
                )
            report = {
                "model": provider.model_id,
                "negative_cases": negatives,
                "cases": results,
                "recall5": sum(case["rank"] is not None for case in results)
                / len(results),
                "recall1": sum(case["rank"] == 1 for case in results) / len(results),
                "median_seconds": median(case["seconds"] for case in results),
            }
        finally:
            session.rollback()
            session.execute(delete(Organisation).where(Organisation.id.in_(ids)))
            session.commit()
            for key, value in [
                ("OBJECT_STORAGE_LOCAL_DIR", old_folder),
                ("OBJECT_STORAGE_BACKEND", old_backend),
            ]:
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    output = Path(
        os.environ.get("METIS_RETRIEVAL_EVAL_OUTPUT", ".agent/semantic-evaluation.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if report["recall5"] < 0.9:
        raise SystemExit("Semantic Recall@5 did not meet the 90% fixture target")


def main():
    if os.environ.get("METIS_SEMANTIC_EVAL_ENABLED") != "true" or not os.environ.get(
        "DATABASE_URL"
    ):
        raise SystemExit(
            "Set METIS_SEMANTIC_EVAL_ENABLED=true and DATABASE_URL to an isolated migrated PostgreSQL database"
        )
    if database.engine.dialect.name != "postgresql":
        raise SystemExit("Semantic retrieval evaluation requires PostgreSQL")
    asyncio.run(evaluate())


if __name__ == "__main__":
    main()
