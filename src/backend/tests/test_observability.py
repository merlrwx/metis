import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from backend.main import app
from backend.models import IngestionJob, Organisation
from backend.reconcile import reconcile_pending_jobs
from fastapi.testclient import TestClient

from backend import database, observability, worker_monitor


def test_metrics_and_correlation_do_not_expose_request_content():
    client = TestClient(app)
    response = client.get("/api/info", headers={"X-Request-ID": "test-correlation"})
    assert response.headers["X-Request-ID"] == "test-correlation"
    response = client.get(
        "/sensitive-unknown-path", headers={"X-Request-ID": "bad value"}
    )
    assert response.headers["X-Request-ID"] != "bad value"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "metis_requests_total" in metrics.text
    assert 'route="unmatched"' in metrics.text
    assert "sensitive-unknown-path" not in metrics.text
    assert "metis_queue_depth 0.0" in metrics.text
    record = logging.LogRecord("metis", logging.INFO, "", 1, "job_started", (), None)
    record.fields = {"job_id": "job-123"}
    assert (
        json.loads(observability.JsonFormatter().format(record))["job_id"] == "job-123"
    )


@pytest.mark.asyncio
async def test_reconcile_only_stale_pending_jobs_and_preserve_scope():
    with database.SessionLocal() as session:
        tenant_a, tenant_b = Organisation(name="A"), Organisation(name="B")
        session.add_all([tenant_a, tenant_b])
        session.flush()
        old = datetime.now(UTC) - timedelta(minutes=10)
        stale_a = IngestionJob(
            organisation_id=tenant_a.id, status="pending", created_at=old
        )
        stale_b = IngestionJob(
            organisation_id=tenant_b.id, status="pending", created_at=old
        )
        queued = IngestionJob(
            organisation_id=tenant_a.id, status="queued", created_at=old
        )
        recent = IngestionJob(organisation_id=tenant_a.id, status="pending")
        session.add_all([stale_a, stale_b, queued, recent])
        session.commit()
        publisher = AsyncMock()
        assert await reconcile_pending_jobs(session, publisher) == 2
        assert {(call.args[0], call.args[1]) for call in publisher.call_args_list} == {
            (str(stale_a.id), str(tenant_a.id)),
            (str(stale_b.id), str(tenant_b.id)),
        }
        assert recent.status == "pending"
        assert queued.status == "queued"
        assert await reconcile_pending_jobs(session, publisher) == 0


@pytest.mark.asyncio
async def test_reconcile_rolls_back_on_unavailable_queue():
    with database.SessionLocal() as session:
        tenant = Organisation(name="A")
        session.add(tenant)
        session.flush()
        job = IngestionJob(
            organisation_id=tenant.id,
            status="pending",
            created_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        session.add(job)
        session.commit()
        with pytest.raises(RuntimeError):
            await reconcile_pending_jobs(
                session, AsyncMock(side_effect=RuntimeError("offline"))
            )
        session.refresh(job)
        assert job.status == "pending"


@pytest.mark.asyncio
async def test_worker_heartbeat_starts_and_cleans_up(monkeypatch):
    redis = AsyncMock()
    monkeypatch.setattr(worker_monitor.Redis, "from_url", lambda *args, **kwargs: redis)
    monkeypatch.setenv("METIS_WORKER_METRICS_PORT", "0")
    state = SimpleNamespace()
    await worker_monitor.start(state)
    await asyncio.sleep(0)
    redis.set.assert_awaited_once_with(state.heartbeat_key, "1", ex=30)
    await worker_monitor.stop(state)
    redis.delete.assert_awaited_once_with(state.heartbeat_key)
    redis.aclose.assert_awaited_once()
