"""Periodically synchronize operator-configured, opted-in Microsoft 365 sources."""

import asyncio

from botocore.exceptions import BotoCoreError, ClientError
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend import database, observability
from backend.connectors.base import ConnectorError
from backend.models import Source
from backend.queue import broker
from backend.services.source_sync import sync_source
from backend.tasks import process_ingestion_job


async def run():
    observability.configure_logging()
    with database.SessionLocal() as session:
        sources = [
            (source.organisation_id, source.id)
            for source in session.scalars(
                select(Source).where(Source.type == "microsoft365")
            )
            if source.configuration.get("sync_enabled") is True
        ]
    await broker.startup()
    failures = 0
    try:
        for organisation_id, source_id in sources:
            try:
                await sync_source(organisation_id, source_id, process_ingestion_job.kiq)
            except (
                ConnectorError,
                SQLAlchemyError,
                OSError,
                ValueError,
                RedisError,
                RuntimeError,
                BotoCoreError,
                ClientError,
            ) as error:
                failures += 1
                observability.log_event(
                    "periodic_source_sync_failed",
                    source_id=str(source_id),
                    organisation_id=str(organisation_id),
                    error_type=type(error).__name__,
                )
    finally:
        await broker.shutdown()
    if failures:
        raise SystemExit(f"{failures} configured source synchronization(s) failed")


def main():
    asyncio.run(run())
