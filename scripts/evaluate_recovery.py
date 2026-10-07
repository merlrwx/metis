"""Opt-in local synthetic PostgreSQL/original-object restore and Redis reconciliation."""

import asyncio
import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from backend.models import Chunk, Document, DocumentVersion, IngestionJob, Organisation
from backend.queue import broker
from backend.reconcile import reconcile_pending_jobs
from backend.services import documents
from backend.storage import LocalObjectStorage
from backend.tasks import process_ingestion_job, run_ingestion_job
from redis.asyncio import Redis
from sqlalchemy import select

from backend import database

COMPOSE = ["docker", "compose", "exec", "-T", "database"]


def database_command(*args, **kwargs):
    return subprocess.run([*COMPOSE, *args], check=True, **kwargs)


async def evaluate():
    suffix = uuid4().hex[:12]
    names = [f"metis_recovery_{suffix}_source", f"metis_recovery_{suffix}_restore"]
    created = []
    with TemporaryDirectory() as folder:
        root = Path(folder)
        original = root / "originals"
        restored = root / "restored"
        os.environ["EMBEDDING_PROVIDER"] = "hashing"
        os.environ["OBJECT_STORAGE_BACKEND"] = "local"
        os.environ["OBJECT_STORAGE_LOCAL_DIR"] = str(original)
        try:
            for name in names:
                database_command("createdb", "-U", "metis", name)
                created.append(name)
            source_url = (
                f"postgresql+psycopg://metis:metis-local-only@localhost:5432/{names[0]}"
            )
            environment = {**os.environ, "DATABASE_URL": source_url}
            await asyncio.to_thread(
                subprocess.run,
                ["alembic", "-c", "src/backend/alembic.ini", "upgrade", "head"],
                env=environment,
                check=True,
            )
            database.configure_database(source_url)
            tenant = uuid4()
            with database.SessionLocal() as session:
                session.add(Organisation(id=tenant, name="Synthetic recovery"))
                session.commit()
                storage = LocalObjectStorage(original)
                _, job, _ = documents.upload_document(
                    session,
                    tenant,
                    "policy.txt",
                    "text/plain",
                    b"Policy revision one: keep records for seven years.",
                    storage,
                )
                await run_ingestion_job(str(job.id), str(tenant))
                _, job, _ = documents.upload_document(
                    session,
                    tenant,
                    "policy.txt",
                    "text/plain",
                    b"Policy revision two: keep records for eight years.",
                    storage,
                )
                await run_ingestion_job(str(job.id), str(tenant))
                _, waiting, _ = documents.upload_document(
                    session,
                    tenant,
                    "pending.txt",
                    "text/plain",
                    b"Pending recovery fixture: open an incident tracking ticket.",
                    storage,
                )
                pending_id = waiting.id
                versions = list(session.scalars(select(DocumentVersion)))
                expected = {
                    (version.id, version.checksum, version.object_key)
                    for version in versions
                }
                current_versions = {
                    doc.id: doc.current_version_id
                    for doc in session.scalars(select(Document))
                }
                expected_chunks = len(list(session.scalars(select(Chunk))))
            database.engine.dispose()
            dump = root / "metadata.dump"
            with dump.open("wb") as output:
                database_command(
                    "pg_dump", "-U", "metis", "-d", names[0], "-Fc", stdout=output
                )
            dump.chmod(0o600)
            archive = root / "originals.tar"
            with tarfile.open(archive, "w") as output:
                output.add(original, arcname="objects")
            archive.chmod(0o600)
            with dump.open("rb") as source:
                database_command(
                    "pg_restore",
                    "-U",
                    "metis",
                    "-d",
                    names[1],
                    "--exit-on-error",
                    "--no-owner",
                    stdin=source,
                )
            with tarfile.open(archive) as source:
                source.extractall(restored, filter="data")
            database.configure_database(
                f"postgresql+psycopg://metis:metis-local-only@localhost:5432/{names[1]}"
            )
            os.environ["OBJECT_STORAGE_LOCAL_DIR"] = str(restored / "objects")
            storage = LocalObjectStorage(restored / "objects")
            with database.SessionLocal() as session:
                versions = list(session.scalars(select(DocumentVersion)))
                assert {
                    (version.id, version.checksum, version.object_key)
                    for version in versions
                } == expected
                assert all(
                    hashlib.sha256(storage.get(version.object_key)).hexdigest()
                    == version.checksum
                    for version in versions
                )
                assert {
                    doc.id: doc.current_version_id
                    for doc in session.scalars(select(Document))
                } == current_versions
                assert len(list(session.scalars(select(Chunk)))) == expected_chunks
            redis = Redis.from_url(os.environ["REDIS_URL"])
            before = {row[0] for row in await redis.xrange(broker.queue_name)}
            await broker.startup()
            try:
                with database.SessionLocal() as session:
                    assert (
                        await reconcile_pending_jobs(
                            session, process_ingestion_job.kiq, minimum_age_seconds=0
                        )
                        == 1
                    )
                    assert session.get(IngestionJob, pending_id).status == "queued"
                added = {
                    row[0] for row in await redis.xrange(broker.queue_name)
                } - before
                assert len(added) == 1
                # Existing job handler reads the restored original; no application Redis0 touched.
                await run_ingestion_job(str(pending_id), str(tenant))
                with database.SessionLocal() as session:
                    assert session.get(IngestionJob, pending_id).status == "indexed"
                await redis.xdel(broker.queue_name, *added)
            finally:
                await broker.shutdown()
                await redis.aclose()
            print(
                json.dumps(
                    {
                        "restored_versions": len(expected),
                        "original_checksums_match": True,
                        "restored_chunks": expected_chunks,
                        "redis_database": 15,
                        "reconciled_jobs": 1,
                        "restored_pending_original_indexed": True,
                    }
                )
            )
        finally:
            database.engine.dispose()
            for name in reversed(created):
                database_command("dropdb", "-U", "metis", "--if-exists", name)


def main():
    if (
        os.environ.get("METIS_RECOVERY_EVAL_ENABLED") != "true"
        or os.environ.get("REDIS_URL") != "redis://localhost:6379/15"
    ):
        raise SystemExit(
            "Run inside the local DevPod with METIS_RECOVERY_EVAL_ENABLED=true and REDIS_URL=redis://localhost:6379/15; do not run alongside other Redis15 tests"
        )
    asyncio.run(evaluate())


if __name__ == "__main__":
    main()
