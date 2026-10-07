"""Operational metrics contain bounded labels and no tenant content."""

import json
import logging
import os
from contextvars import ContextVar
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, select

PIPELINE_EVENTS = Counter(
    "metis_pipeline_events_total",
    "Pipeline stage results without tenant content",
    ["stage", "result"],
)
ANSWER_OUTCOMES = Counter(
    "metis_answer_outcomes_total", "Grounded answer outcomes", ["outcome"]
)

LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

request_id = ContextVar("request_id", default="")
logger = logging.getLogger("metis")
REQUESTS = Counter(
    "metis_requests_total", "API requests", ["method", "route", "status"]
)
ERRORS = Counter("metis_errors_total", "API errors", ["route", "status"])
REQUEST_DURATION = Histogram(
    "metis_request_duration_seconds", "API latency", ["route"], buckets=LATENCY_BUCKETS
)
RAG_DURATION = Histogram(
    "metis_rag_query_duration_seconds",
    "Grounded answer latency",
    buckets=LATENCY_BUCKETS,
)
LLM_DURATION = Histogram(
    "metis_llm_request_duration_seconds",
    "Model request latency",
    buckets=LATENCY_BUCKETS,
)
RETRIEVAL_DURATION = Histogram(
    "metis_retrieval_duration_seconds", "Vector query latency"
)
DB_DURATION = Histogram(
    "metis_database_query_duration_seconds", "Database query latency"
)
DB_SLOW = Counter("metis_database_slow_queries_total", "Queries over one second")
JOBS_STARTED = Counter("metis_jobs_started_total", "Worker attempts started")
JOBS_COMPLETED = Counter("metis_jobs_completed_total", "Successful worker attempts")
JOBS_FAILED = Counter("metis_jobs_failed_total", "Failed worker attempts")
RETRIES = Counter("metis_retry_count_total", "Retried ingestion attempts")
JOB_DURATION = Histogram(
    "metis_job_duration_seconds", "Worker attempt latency", buckets=LATENCY_BUCKETS
)
DOCUMENTS = Counter("metis_documents_processed_total", "Indexed document versions")
CHUNKS = Counter("metis_chunks_created_total", "Indexed chunks")
EMBEDDINGS = Counter("metis_embedding_requests_total", "Embedding provider calls")
QUEUE_DEPTH = Gauge("metis_queue_depth", "Pending or queued database jobs")
OLDEST_JOB = Gauge(
    "metis_oldest_message_age_seconds", "Age of oldest waiting database job"
)
ACTIVE_WORKERS = Gauge("metis_active_workers", "Workers with a recent Redis heartbeat")
DB_SIZE = Gauge("metis_database_size_bytes", "PostgreSQL database size")
DB_CONNECTIONS = Gauge("metis_database_connections", "Current database connections")
RECONCILED = Counter("metis_jobs_reconciled_total", "Orphaned pending jobs republished")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                "request_id": request_id.get(),
                **getattr(record, "fields", {}),
            }
        )


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_event(event, **fields):
    logger.info(event, extra={"fields": fields})


async def request_metrics(request, call_next):
    supplied = request.headers.get("X-Request-ID", "")
    correlation = (
        supplied
        if 0 < len(supplied) <= 64
        and all(c.isascii() and (c.isalnum() or c in "-_") for c in supplied)
        else str(uuid4())
    )
    token = request_id.set(correlation)
    start = perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = correlation
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        method = (
            request.method
            if request.method
            in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
                "TRACE",
                "CONNECT",
            }
            else "OTHER"
        )
        if route != "/metrics":
            REQUESTS.labels(method, route, str(status)).inc()
            REQUEST_DURATION.labels(route).observe(perf_counter() - start)
            if status >= 400:
                ERRORS.labels(route, str(status)).inc()
            log_event(
                "http_request",
                method=method,
                route=route,
                status=status,
                duration_seconds=perf_counter() - start,
            )
        request_id.reset(token)


def refresh_database_metrics(session):
    from sqlalchemy import text

    from backend.models import IngestionJob

    count, oldest = session.execute(
        select(func.count(), func.min(IngestionJob.created_at)).where(
            IngestionJob.status.in_(["pending", "queued"])
        )
    ).one()
    QUEUE_DEPTH.set(count)
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    OLDEST_JOB.set(
        max(0, (datetime.now(UTC) - oldest).total_seconds()) if oldest else 0
    )
    if session.bind.dialect.name == "postgresql":
        DB_SIZE.set(session.scalar(text("SELECT pg_database_size(current_database())")))
        DB_CONNECTIONS.set(
            session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                )
            )
        )

    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        from redis import Redis
        from redis.exceptions import RedisError

        ACTIVE_WORKERS.set(0)
        try:
            with Redis.from_url(
                redis_url, socket_timeout=2, socket_connect_timeout=2
            ) as client:
                ACTIVE_WORKERS.set(
                    sum(1 for _ in client.scan_iter(match="metis:worker:*"))
                )
        except RedisError:
            log_event("queue_metrics_unavailable")
