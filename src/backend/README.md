# Metis API and worker

Run from the repository root:

```bash
docker compose up -d database redis
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
  uv run --locked --project src/backend alembic -c src/backend/alembic.ini upgrade head
export METIS_AUTH_SECRET_KEY="$(openssl rand -hex 32)"
export METIS_BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"
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
- `POST /api/auth/register`: create an account with a password stored as an Argon2 hash.
- `POST /api/auth/token`: exchange OAuth2 form fields `username` (email) and `password` for a 30-minute bearer token.
- `GET /api/auth/me`: return the signed-in user and organization memberships.
- `POST /api/organisations`: create an organisation and become its owner.
- `POST /api/organisations/{id}/claim`: claim an existing unowned organisation with `X-Metis-Bootstrap-Token`; only organisations without members can be claimed.
- `GET/POST /api/organisations/{id}/members`: owners and admins can list members and add registered users. Only owners may grant the admin role.
- `GET /api/organisations/{id}/audit-events`: list the latest 100 tenant-scoped events; owners and admins only.
- `POST /api/organisations/{id}/sources`: create a tenant-owned source.
- `POST /api/organisations/{id}/documents`: create document metadata, with an optional tenant-owned source.
- `POST /api/organisations/{id}/documents/upload`: upload a PDF, DOCX, TXT, or Markdown file; returns its document and queued ingestion job.
- `GET /api/organisations/{id}/documents`: list documents and their current ingestion status, scoped to that organisation.
- `GET /api/organisations/{id}/documents/{document_id}`: read document metadata and ingestion status within that organisation.
- `POST /api/organisations/{id}/search`: return the closest current document chunks with source, document, page and section metadata. Optional `source_id`, `document_id` and `limit` fields narrow the results.
- `POST /api/organisations/{id}/chat`: answer a message from retrieved chunks, with evidence citations and a reusable `conversation_id`.
- `GET /api/organisations/{id}/conversations/{conversation_id}`: load a saved conversation and its most recent messages; `limit` defaults to 50 and is capped at 100.
- `POST /api/jobs/test`: enqueue a trivial worker job for an `organisation_id`; an optional `Idempotency-Key` reuses the same job.
- `GET /api/organisations/{organisation_id}/jobs/{job_id}`: read job state within that organisation.
- `/docs`: interactive OpenAPI documentation.

Every organization route requires `Authorization: Bearer <token>` and a current membership. Owners and admins can create sources/documents, upload, enqueue the test job and manage members. Members can read documents and jobs, search and chat. Conversation transcripts are restricted to their creator, except organization owners and admins. Audit entries record actions and resource identifiers, and never store prompts or document content. Document object keys retain their `organisations/{organisation_id}/...` prefix.

Taskiq publishes job IDs to a Redis Stream. PostgreSQL stores job status, attempts and errors. The worker retries failures up to three attempts. On startup it writes a wake-up event so a replacement consumer checks and reclaims unacknowledged stream entries. The worker extracts PDF pages, DOCX headings/tables, Markdown headings, and UTF-8 text; it creates 1000-character chunks with 150-character overlap and stores the matching embeddings, page/section offsets, and current model ID in the same transaction. A successful document job reports `indexed`; the generic test job reports `completed`. `mise exec -- bash scripts/test-queue` exercises worker recovery, retries, upload, and tenant-filtered vector retrieval against PostgreSQL and Redis.

PostgreSQL must have the pgvector extension; Compose and dev k3d use `pgvector/pgvector:pg17`. Search uses exact cosine distance and does not add an approximate index. The local `hashing` provider creates deterministic lexical vectors for development and CI only. Configure `EMBEDDING_PROVIDER=openai-compatible`, `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, and `EMBEDDING_API_KEY` (or `OPENAI_API_KEY`) to call a compatible embeddings API; the model must return 1536 dimensions.

Chat uses LangChain `ChatOpenAI` through a provider interface. Configure `GPTMOCK_BASE_URL` (default `http://127.0.0.1:8000/v1`), `GPTMOCK_MODEL` (default `gpt-6-luna`), `GPTMOCK_API_KEY` (default placeholder `chatmock`), `GPTMOCK_TIMEOUT` (default 120 seconds), and `GPTMOCK_MAX_RETRIES` (default 1). The RAG path requires a retrieval score of at least 0.2 and a valid citation marker; otherwise it returns a fixed no-evidence response without citing chunks. The API records model identity, input/output token counts, citations, message order, and the conversation owner.

Object storage can use local files (`OBJECT_STORAGE_BACKEND=local`) or an S3-compatible bucket (`OBJECT_STORAGE_BACKEND=s3`, `S3_BUCKET`, optional `S3_ENDPOINT_URL`, `AWS_REGION`, and AWS credentials). Use a shared local directory for API and worker processes; Compose mounts one named volume for both. Uploads are limited to 20 MiB and checked against their declared MIME type and file format.

Set `METIS_AUTH_SECRET_KEY` to a unique secret of at least 32 characters and `METIS_BOOTSTRAP_TOKEN` to a separate secret. The local Compose and k3d values are for disposable development only and must be replaced for shared deployments. Use the bootstrap token only to attach an authenticated account to existing unowned data. Standard CI mocks all chat calls. The Compose database password is for local use only.
