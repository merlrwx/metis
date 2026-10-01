# Metis API and worker

Run from the repository root:

```bash
docker compose up -d database redis
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
  uv run --locked --project src/backend alembic -c src/backend/alembic.ini upgrade head
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
REDIS_URL=redis://localhost:6379/0 \
  uv run --locked --project src/backend metis-api
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
REDIS_URL=redis://localhost:6379/0 \
  uv run --locked --project src/backend metis-worker
```

The API container applies Alembic migrations before serving requests. `HOST` defaults to `0.0.0.0`; `PORT` defaults to `8000`. Configure `DATABASE_URL` and `REDIS_URL` for local services. API and worker must share the same `OBJECT_STORAGE_LOCAL_DIR` when `OBJECT_STORAGE_BACKEND=local`.

- `GET /health`: database connectivity.
- `GET /api/info`: application identity and current stage.
- `POST /api/organisations`: create an organisation.
- `POST /api/organisations/{id}/sources`: create a tenant-owned source.
- `POST /api/organisations/{id}/documents`: create document metadata, with an optional tenant-owned source.
- `POST /api/organisations/{id}/documents/upload`: upload a PDF, DOCX, TXT, or Markdown file; returns its document and queued ingestion job.
- `GET /api/organisations/{id}/documents`: list documents and their current ingestion status, scoped to that organisation.
- `GET /api/organisations/{id}/documents/{document_id}`: read document metadata and ingestion status within that organisation.
- `POST /api/jobs/test`: enqueue a trivial worker job for an `organisation_id`; an optional `Idempotency-Key` reuses the same job.
- `GET /api/organisations/{organisation_id}/jobs/{job_id}`: read job state within that organisation.
- `/docs`: interactive OpenAPI documentation.

Taskiq publishes job IDs to a Redis Stream. PostgreSQL stores job status, attempts and errors. The worker retries failures up to three attempts. On startup it writes a wake-up event so a replacement consumer checks and reclaims unacknowledged stream entries. The worker extracts PDF pages, DOCX headings/tables, Markdown headings, and UTF-8 text, then stores normalized text plus page/section offsets with the document version. A successful document job reports `indexed`; the generic test job reports `completed`. `mise exec -- bash scripts/test-queue` exercises worker recovery, retries, and PDF ingestion against PostgreSQL and Redis.

Object storage can use local files (`OBJECT_STORAGE_BACKEND=local`, default) or an S3-compatible bucket (`OBJECT_STORAGE_BACKEND=s3`, `S3_BUCKET`, optional `S3_ENDPOINT_URL`, `AWS_REGION`, and AWS credentials). Use a shared local directory for API and worker processes; Compose mounts one named volume for both. Uploads are limited to 20 MiB and checked against their declared MIME type and file format.

The development API does not yet require authentication; tenant authentication and membership enforcement are planned in Phase 6. The Compose password is for local use only.
