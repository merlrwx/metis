# Metis

Metis will let organisations upload internal knowledge and ask questions with citations to the original documents. The implementation plan is in [plans/plan1.md](plans/plan1.md).

The application follows the delivery foundation established in Phase 0 and reuses [devops-app](https://github.com/merlrwx/devops-app): independent uv projects, FastAPI, Streamlit, mise, multi-stage non-root Docker images, Ruff/pre-commit, pytest coverage, Trivy, Release Please, GHCR, k3d and Flux setup tools.

The current milestone adds PostgreSQL-backed tenant metadata, a Redis Streams worker using Taskiq, document ingestion, exact vector retrieval with pgvector, grounded chat, and tenant authentication. Organisations can upload PDF, DOCX, TXT, or Markdown files; the worker extracts text, splits it into overlapping chunks, and stores 1536-dimensional embeddings with page or section offsets. Search and chat apply organisation and current-version filters. Answers cite retrieved chunks, unsupported questions receive a fixed refusal, and conversations store messages, citations, model identity, and token usage. Signed bearer tokens and organization memberships guard tenant routes; owners and admins manage members, and important changes are audited without storing chat prompts.

## Development with DevPod

```bash
devpod up . --id metis --ide none
devpod ssh metis
cd /workspaces/metis
mise exec -- ./scripts/verify
```

The devcontainer keeps the base project's Docker-in-Docker feature. Setup installs the mise tools and locked dependencies for all three uv projects. [DevPod also supports creating a workspace from a local folder or Git repository](https://devpod.sh/docs/developing-in-workspaces/create-a-workspace).

Start PostgreSQL and Redis for local API and worker development. The local object store defaults to `/tmp/metis-objects`; the API and worker must share that directory. `docker compose up --build --wait` configures a shared named volume automatically.

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

Set `METIS_AUTH_SECRET_KEY` to a unique random value of at least 32 characters before starting the API. Set `METIS_BOOTSTRAP_TOKEN` to a separate random value; it is only used to claim existing organisations that have no members. Compose supplies local-only defaults, which must be replaced for any shared deployment.

Run Streamlit in another terminal with `uv run --locked --project src/frontend streamlit run src/frontend/app.py`. The API is on port 8000 (`/docs` for OpenAPI), and Streamlit is on port 8501. Set `BACKEND_URL` if the API runs elsewhere. DevPod forwards these ports. The UI supports registration and sign-in, organisation selection, document upload and status, source management, cited chat, and member settings. You can use its Create account flow and create an organisation after signing in, or use the API directly:

```bash
curl -X POST http://localhost:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.test","name":"Your Name","password":"a-long-local-password"}'
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=you@example.test&password=a-long-local-password' | jq -r .access_token)
ORGANISATION_ID=$(curl -s -X POST http://localhost:8000/api/organisations \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Example organisation"}' | jq -r .id)
```

Every `/api/organisations/{id}/...` request needs `Authorization: Bearer $TOKEN`; organization membership is checked on each request. Owners and admins can add already-registered users and upload documents; members can read, search and chat. An owner alone can grant the admin role. To attach an existing organization with no memberships, register and sign in first, then call `POST /api/organisations/{id}/claim` with both the bearer token and `X-Metis-Bootstrap-Token`. Existing organizations that already have a membership cannot be claimed. Upload a file with multipart field `file`; poll the returned job or read the document’s `ingestion_status`.

```bash
ORGANISATION_ID=your-org-uuid
curl -H "Authorization: Bearer $TOKEN" -F 'file=@policy.pdf' \
  "http://localhost:8000/api/organisations/${ORGANISATION_ID}/documents/upload"
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"medication incident","limit":5}' \
  "http://localhost:8000/api/organisations/${ORGANISATION_ID}/search"
```

Reuse an `Idempotency-Key` header with `POST /api/jobs/test` to retrieve the same test job.

With the GPTMock bridge available, continue a grounded conversation through `POST /api/organisations/{organisation_id}/chat` using `{"message":"What do we do after a medication incident?"}`. Reuse the returned `conversation_id` for follow-up turns; load the saved transcript from `GET /api/organisations/{organisation_id}/conversations/{conversation_id}`. Conversations belong to their creator; organization owners and admins can inspect all organization conversations.

Compose, k3d development, and CI use the deterministic `hashing` embedding provider; it is for local retrieval checks and is not a semantic model. For semantic embeddings, configure an OpenAI-compatible embeddings endpoint with `EMBEDDING_PROVIDER=openai-compatible`, `EMBEDDING_BASE_URL` (defaults to `https://api.openai.com/v1`), `EMBEDDING_MODEL` (defaults to `text-embedding-3-small`), and `EMBEDDING_API_KEY` or `OPENAI_API_KEY`. The configured model must return 1536 values per embedding. No standard CI job calls an embedding or chat service.

For an S3-compatible object store, provision the bucket first, then set `OBJECT_STORAGE_BACKEND=s3`, `S3_BUCKET`, and optionally `S3_ENDPOINT_URL` and `AWS_REGION`; provide credentials through the standard AWS environment variables or the runtime’s credential provider. Keep the local backend for tests and single-workspace development. The Dev k3d overlay mounts one temporary host directory into its nodes for disposable local testing.

Alternatively, `mise exec -- bash scripts/test-postgres` applies migrations and runs backend tests against PostgreSQL. `mise exec -- bash scripts/test-queue` also runs Redis worker recovery, retries, PDF upload-to-indexed, tenant-filtered vector search, and mocked multi-turn cited chat checks using an isolated Redis database and temporary local object store. `docker compose up --build --wait` starts PostgreSQL with pgvector, Redis, API, worker, and frontend. The default database password is local-only; set `POSTGRES_PASSWORD` for a personal deployment and do not reuse it elsewhere. Audit events record organization creation/claim, source and document changes, member additions, and chat turns; they contain identifiers and operational details, not prompts or document text.

## Verification

```bash
mise exec -- ./scripts/verify
mise exec -- pre-commit run --all-files
mise exec -- uv run --locked --project kubernetes python kubernetes/e2e_test.py
```

`verify` checks Ruff lint and formatting, backend and frontend tests with at least 80% coverage, dependency locks, Compose configuration and rendered Kubernetes manifests. Frontend tests run Streamlit's AppTest with mocked backend responses; normal CI uses local hashing embeddings and never calls an external model.

E2E builds both Docker images and runs the Metis services in a disposable `metis-cluster` k3d cluster. It checks database migrations, bearer authentication, tenant metadata persistence, document upload, chunk extraction and vector search, a queued worker job and idempotency, API and worker scaling, graceful worker termination, removed study-tracker routes, and frontend HTTP health. Successful runs delete the test cluster; failures retain it for inspection. E2E operates only on the local Docker-provider environment. See [kubernetes/README.md](kubernetes/README.md).

## Delivery and credentials

Backend and frontend checks, coverage, container builds and Trivy scans run on PRs and pushes to main. Kubernetes E2E also runs on PRs and main. Container release workflows publish `ghcr.io/merlrwx/metis-api` and `ghcr.io/merlrwx/metis-web` when `backend*` and `frontend*` tags are pushed.

Release Please retains separate backend/frontend releases, starting at 0.1.0. Set the `METIS_RELEASE_TOKEN` repository secret to a token allowed to create release PRs and tags, then set the repository variable `ENABLE_RELEASE` to `true`. A dedicated token is needed for release tags to trigger downstream image workflows. The new repository does not inherit secrets from devops-app.

GitOps updates are opt-in via repository variable `ENABLE_GITOPS=true`. Before enabling:

1. Provision the separate `merlrwx/metis-gitops` repo with Flux `clusters/dev`, `apps/dev` and `apps/prod` overlays using the Metis namespace and image names. This bootstrap does not create that repo or deploy to a shared cluster.
2. Add `GITOPS_DEPLOY_KEY`, a write-enabled SSH private key for that repo, and `METIS_RELEASE_TOKEN`, a token permitted to open its production promotion PRs.
3. Use the preserved `mise run setup-keys` and `mise run setup-cluster-gitops` tools only when ready to create the GitOps repo and bootstrap Flux. Keys stay in ignored `dev-keys/` and use Metis names.

The reusable workflow updates dev image tags and opens a prod promotion PR. GitOps and live Flux reconciliation remain unverified until that separate repo and credentials are configured. The existing devops-app GitOps environment is unchanged.

## Local LLM testing

Metis uses LangChain `ChatOpenAI` with the existing GPTMock bridge. From the workstation, forward the read-only service to port 8001:

```bash
kubectl -n hermes port-forward svc/chatmock 8001:8000
```

In a second workstation terminal, open a reverse tunnel into the DevPod:

```bash
devpod ssh metis --reverse-forward-ports 8001:127.0.0.1:8001
```

In that DevPod shell, run:

```bash
mise run test-gptmock
```

The smoke test uses `gpt-5.6-luna`, which is currently advertised by the bridge. Set `GPTMOCK_MODEL` to another model returned by `/v1/models` if needed. The key defaults to the bridge's placeholder `chatmock`; no key is printed. The Compose API receives the same settings and defaults to `host.docker.internal:8001`; set `GPTMOCK_BASE_URL` to a URL reachable from its container if your DevPod network uses a different route. Recreate the backend after changing these settings with `docker compose up -d --force-recreate backend`. The adapter uses Chat Completions (`use_responses_api=False`). Standard tests and CI replace the provider and make no live model calls.

The GitOps dev overlay uses `http://chatmock.hermes.svc.cluster.local:8000/v1` from inside the homelab cluster. The disposable k3d overlay never calls a live model during its tests; set `GPTMOCK_BASE_URL` to a reachable endpoint before using chat there. A DevPod has its own network namespace: its localhost is not the workstation. When running the backend directly inside the DevPod, use `http://127.0.0.1:8001/v1` through the reverse tunnel above.

LangGraph remains deferred until a real branching workflow requires it, as specified by the architectural plan.
