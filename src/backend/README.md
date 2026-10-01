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

The API container applies Alembic migrations before serving requests. `HOST` defaults to `0.0.0.0`; `PORT` defaults to `8000`. Configure `DATABASE_URL` and `REDIS_URL` for local services.

- `GET /health`: database connectivity.
- `GET /api/info`: application identity and current stage.
- `POST /api/organisations`: create an organisation.
- `POST /api/organisations/{id}/sources`: create a tenant-owned source.
- `POST /api/organisations/{id}/documents`: create document metadata, with an optional tenant-owned source.
- `GET /api/organisations/{id}/documents`: list metadata scoped to that organisation.
- `POST /api/jobs/test`: enqueue a trivial worker job for an `organisation_id`; an optional `Idempotency-Key` reuses the same job.
- `GET /api/organisations/{organisation_id}/jobs/{job_id}`: read job state within that organisation.
- `/docs`: interactive OpenAPI documentation.

Taskiq publishes job IDs to a Redis Stream. PostgreSQL stores job status, attempts and errors. The worker retries failures up to three attempts. On startup it writes a wake-up event so a replacement consumer checks and reclaims unacknowledged stream entries. `mise exec -- bash scripts/test-queue` exercises worker recovery and retries against PostgreSQL and Redis.

The development API does not yet require authentication; tenant authentication and membership enforcement are planned in Phase 6. The Compose password is for local use only.
