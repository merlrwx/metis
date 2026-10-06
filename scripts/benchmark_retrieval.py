"""Opt-in exact retrieval benchmark; creates and removes only synthetic tenants."""

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import uuid4

from backend.embeddings import EMBEDDING_DIMENSIONS, HashingEmbeddingProvider
from backend.models import Chunk, Document, DocumentVersion, Organisation
from backend.services.knowledge import search_chunks
from sqlalchemy import delete, insert, text

from backend import database


def seed(session, organisation_id, count, embedding, *, current=True):
    document_id, version_id = uuid4(), uuid4()
    session.add(
        Document(
            id=document_id, organisation_id=organisation_id, title="Synthetic benchmark"
        )
    )
    session.flush()
    session.add(
        DocumentVersion(
            id=version_id,
            organisation_id=organisation_id,
            document_id=document_id,
            filename="synthetic.txt",
            checksum=str(version_id),
            object_key="benchmark/not-stored",
            mime_type="text/plain",
            size_bytes=0,
        )
    )
    session.flush()
    if current:
        session.get(Document, document_id).current_version_id = version_id
    for start in range(0, count, 200):
        session.execute(
            insert(Chunk),
            [
                {
                    "id": uuid4(),
                    "organisation_id": organisation_id,
                    "document_version_id": version_id,
                    "chunk_index": i,
                    "content": f"Synthetic policy chunk {i}",
                    "start_offset": 0,
                    "end_offset": 32,
                    "embedding_model": "benchmark-hashing",
                    "embedding": embedding,
                }
                for i in range(start, min(start + 200, count))
            ],
        )
    session.commit()


def measure(session, organisation_id, query, samples=35):
    timings = []
    for i in range(samples + 5):
        started = perf_counter()
        hits = search_chunks(session, organisation_id, query, "benchmark-hashing", 5)
        elapsed = (perf_counter() - started) * 1000
        assert len(hits) == 5
        assert all(
            hit.chunk.organisation_id == organisation_id
            and hit.document.current_version_id == hit.chunk.document_version_id
            for hit in hits
        )
        if i >= 5:
            timings.append(elapsed)
    return {
        "samples": samples,
        "p50_ms": round(median(timings), 2),
        "p95_ms": round(sorted(timings)[math.ceil(samples * 0.95) - 1], 2),
        "max_ms": round(max(timings), 2),
    }


def main():
    if os.environ.get("METIS_BENCHMARK_ENABLED") != "true" or not os.environ.get(
        "DATABASE_URL"
    ):
        raise SystemExit(
            "Set METIS_BENCHMARK_ENABLED=true and DATABASE_URL to an isolated migrated PostgreSQL database"
        )
    if database.engine.dialect.name != "postgresql":
        raise SystemExit("This benchmark requires PostgreSQL with pgvector")
    ids = [uuid4(), uuid4(), uuid4()]
    embedding = HashingEmbeddingProvider().embed_query("Synthetic policy benchmark")
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "dimensions": EMBEDDING_DIMENSIONS,
        "method": "exact cosine, top 5, 5 warmups, sequential calls, synthetic rows",
        "cases": [],
    }
    with database.SessionLocal() as session:
        try:
            report["postgresql"] = session.scalar(text("SELECT version()"))
            for organisation_id in ids:
                session.add(
                    Organisation(
                        id=organisation_id, name="Metis synthetic retrieval benchmark"
                    )
                )
            session.commit()
            seed(session, ids[2], 1000, embedding)  # Foreign tenant, equally relevant.
            for organisation_id, count in zip(ids[:2], (1000, 5000)):
                seed(session, organisation_id, count, embedding)
                seed(session, organisation_id, 100, embedding, current=False)
                result = measure(session, organisation_id, embedding)
                report["cases"].append(
                    dict(active_chunks=count, stale_chunks=100, **result)
                )
        finally:
            session.rollback()
            session.execute(delete(Organisation).where(Organisation.id.in_(ids)))
            session.commit()
    output = Path(
        os.environ.get("METIS_BENCHMARK_OUTPUT", ".agent/retrieval-benchmark.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
