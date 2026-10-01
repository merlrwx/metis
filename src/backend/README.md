# Metis API and worker

Run from the repository root:

```bash
docker compose up -d database redis
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
  uv run --locked --project src/backend alembic -c src/backend/alembic.ini upgrade head
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
REDIS_URL=redis://localhost:6379/0 \
EMBEDDING_PROVIDER=hashing \
  uv run --locked --project src/backend metis-api
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
REDIS_URL=redis://localhost:6379/0 \
EMBEDDING_PROVIDER=hashing \
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
- `POST /api/organisations/{id}/search`: return the closest current document chunks with source, document, page and section metadata. Optional `source_id`, `document_id` and `limit` fields narrow the results.
- `POST /api/jobs/test`: enqueue a trivial worker job for an `organisation_id`; an optional `Idempotency-Key` reuses the same job.
- `GET /api/organisations/{organisation_id}/jobs/{job_id}`: read job state within that organisation.
- `/docs`: interactive OpenAPI documentation.

Taskiq publishes job IDs to a Redis Stream. PostgreSQL stores job status, attempts and errors. The worker retries failures up to three attempts. On startup it writes a wake-up event so a replacement consumer checks and reclaims unacknowledged stream entries. The worker extracts PDF pages, DOCX headings/tables, Markdown headings, and UTF-8 text; it creates 1000-character chunks with 150-character overlap and stores the matching embeddings, page/section offsets, and current model ID in the same transaction. A successful document job reports `indexed`; the generic test job reports `completed`. `mise exec -- bash scripts/test-queue` exercises worker recovery, retries, upload, and tenant-filtered vector retrieval against PostgreSQL and Redis.

PostgreSQL must have the pgvector extension; Compose and dev k3d use `pgvector/pgvector:pg17`. Search uses exact cosine distance and does not add an approximate index. The local `hashing` provider creates deterministic lexical vectors for development and CI only. Configure `EMBEDDING_PROVIDER=openai-compatible`, `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, and `EMBEDDING_API_KEY` (or `OPENAI_API_KEY`) to call a compatible embeddings API; the model must return 1536 dimensions.

Object storage can use local files (`OBJECT_STORAGE_BACKEND=local`) or an S3-compatible bucket (`OBJECT_STORAGE_BACKEND=s3`, `S3_BUCKET`, optional `S3_ENDPOINT_URL`, `AWS_REGION`, and AWS credentials). Use a shared local directory for API and worker processes; Compose mounts one named volume for both. Uploads are limited to 20 MiB and checked against their declared MIME type and file format.

The development API does not yet require authentication; tenant authentication and membership enforcement are planned in Phase 6. The Compose password is for local use only.
