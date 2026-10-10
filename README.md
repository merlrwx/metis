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
export METIS_SESSION_COOKIE_SECRET="$(openssl rand -hex 32)"
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

Set `METIS_AUTH_SECRET_KEY` to a unique random value of at least 32 characters before starting the API. Set `METIS_SESSION_COOKIE_SECRET` to a separate random value before starting the frontend; it encrypts the browser cookie that keeps your sign-in across page refreshes. Set `METIS_BOOTSTRAP_TOKEN` to another random value; it is only used to claim existing organisations that have no members. Compose supplies local-only defaults, which must be replaced for any shared deployment.

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

Compose, k3d development, and CI use the deterministic `hashing` embedding provider; it is for local retrieval checks and is not a semantic model. For semantic embeddings, configure an OpenAI-compatible embeddings endpoint with `EMBEDDING_PROVIDER=openai-compatible`, `EMBEDDING_BASE_URL` (defaults to `https://api.openai.com/v1`), `EMBEDDING_MODEL` (defaults to `text-embedding-3-small`), and `EMBEDDING_API_KEY` or `OPENAI_API_KEY`. Set `EMBEDDING_DIMENSIONS` to the configured model’s output size (default 1536 for the compatible provider). Existing documents must be re-indexed before changing models; vectors from different models are never interchangeable. See [local semantic setup and measured evaluation](docs/semantic-search.md) for the pinned 384-dimensional CPU model, maintenance recovery and semantic Compose override. No standard CI job calls an embedding or chat service.

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

`mise run verify-manifests` additionally renders this repository’s base and development Kubernetes overlays. Production overlays live in the separate GitOps repository. The autonomous goal's original `scripts/verify` stays stable; run both for deployment configuration changes.

## Delivery and credentials

Backend and frontend checks, coverage, container builds and Trivy scans run on PRs and pushes to main. Kubernetes E2E also runs on PRs and main. Container release workflows publish `ghcr.io/merlrwx/metis-api` and `ghcr.io/merlrwx/metis-web` when `backend*` and `frontend*` tags are pushed.

Release Please retains separate backend/frontend releases, starting at 0.1.0. Set the `METIS_RELEASE_TOKEN` repository secret to a token allowed to create release PRs and tags, then set the repository variable `ENABLE_RELEASE` to `true`. A dedicated token is needed for release tags to trigger downstream image workflows. The new repository does not inherit secrets from devops-app.

GitOps updates are opt-in via repository variable `ENABLE_GITOPS=true`. Before enabling:

1. Provision the separate `merlrwx/metis-gitops` repo with Flux `clusters/dev`, `apps/dev` and `apps/prod` overlays using the Metis namespace and image names. This bootstrap does not create that repo or deploy to a shared cluster.
2. Add `GITOPS_DEPLOY_KEY`, a write-enabled SSH private key for that repo, and `METIS_RELEASE_TOKEN`, a token permitted to open its production promotion PRs.
3. Use the preserved `mise run setup-keys` and `mise run setup-cluster-gitops` tools only when ready to create the GitOps repo and bootstrap Flux. Keys stay in ignored `dev-keys/` and use Metis names.

The reusable workflow updates dev image tags and opens a prod promotion PR. GitOps and live Flux reconciliation remain unverified until that separate repo and credentials are configured. The existing devops-app GitOps environment is unchanged.

## Operations

The API exposes aggregate Prometheus metrics at `/metrics`; worker metrics listen on port 9100 inside the container network. Kubernetes includes a CronJob to reconcile stale pending jobs. See [monitoring/README.md](monitoring/README.md) for dashboard import, scraping, alert rules, metric semantics and recovery steps. Request responses include `X-Request-ID`; structured lifecycle logs identify jobs without recording document text or prompts.

## Local LLM testing

Metis uses LangChain `ChatOpenAI` through an OpenAI-compatible chat API and a separate pinned CPU MiniLM service for semantic retrieval. The documented default local workflow still uses the existing ChatMock bridge for generation. For speed-first experiments, `compose.local-llm.yaml` can replace that bridge with a tiny Ollama model running locally. Normal CI makes no live model calls.

From the workstation, with the existing `metis` DevPod configured, run:

```bash
mise run local
```

This checks the read-only ChatMock service, starts local PostgreSQL/Redis/API/worker/frontend and semantic embeddings in the DevPod, verifies both model services, and opens localhost forwarding. Open **http://localhost:8501**; API documentation is at **http://localhost:8000/docs**. Existing uploaded data and volumes are retained. The command can be repeated. It uses `homelab-k8s`, namespace `hermes`, service `chatmock` by default; set `METIS_CHATMOCK_CONTEXT` and `METIS_CHATMOCK_NAMESPACE` for your existing bridge. Forwarding logs are under ignored `.agent/local/`. It requires available host ports 8000/8501 and a compatible ChatMock listener on 8001; it does not replace occupied listeners.

The host bridge runs on 8001. OpenSSH forwards it into the DevPod on 8002; containers reach `http://host.docker.internal:8002/v1`. API and worker use the semantic Compose override together. See [semantic setup and re-indexing](docs/semantic-search.md) for manual commands, and [local operation and recovery](docs/local-operations.md) for limits, evaluations and backups. For optional Microsoft 365 credentials, invoke `METIS_CONNECTOR_ENV_FILE=/external/path.env bash scripts/local-devpod` inside the DevPod; credentials remain outside Git. No library is required to use uploaded knowledge.

The bridge model defaults to `gpt-5.6-luna`; choose an advertised model with `GPTMOCK_MODEL` in the DevPod environment when running the manual Compose workflow. Chat uses bounded Chat Completions (`use_responses_api=False`). Microsoft 365 live-library validation is deferred because no approved library is configured. Connector fixtures cover its behavior; they do not establish live Microsoft 365 connectivity.

### Fast tiny local chat model

To run chat generation on the local machine instead of the ChatMock bridge, use the local LLM Compose override with the semantic embedding override. It starts Ollama and points the existing OpenAI-compatible chat configuration at `http://llm:11434/v1`. The default model is `smollm2:135m`, chosen for minimum size and latency rather than answer quality.

Pull the tiny model once into the retained `ollama-models` volume:

```bash
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml -f compose.local-llm.yaml up -d llm
mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml -f compose.local-llm.yaml exec llm ollama pull smollm2:135m
```

Then start Metis with local semantic retrieval and local chat:

```bash
LOCAL_LLM_MODEL=smollm2:135m \
  mise exec -- docker compose -f compose.yaml -f compose.semantic.yaml -f compose.local-llm.yaml up --build --wait
```

Swap tiny models without changing Metis code by setting `LOCAL_LLM_MODEL`, for example `smollm2:360m`, `qwen2.5:0.5b` or `tinyllama:1.1b`. The existing `GPTMOCK_*` variables remain the backend chat configuration; the override only supplies local defaults. Chat model changes do not require re-indexing. Embedding model changes still require re-indexing because stored vectors are model-specific.

Expect very small models to be fast but weak: they may ignore instructions, produce terse answers, miss citation markers or trigger Metis' no-evidence fallback more often. Use this mode for local plumbing, latency and UX checks, not for answer-quality validation.

## CI and optional model evaluation

Backend CI runs lint, pre-commit, unit and PostgreSQL/Redis integration tests, image build and Trivy scanning. Kubernetes E2E uses known uploads and deterministic embeddings, verifies tenant isolation and retrieval, tests worker shutdown, and runs the reconciliation CronJob. Monitoring CI validates alert rules, scrape configuration and dashboard JSON. Normal push/PR checks never call a live embedding or chat endpoint.

To evaluate the local bridge explicitly, run `METIS_LIVE_LLM_ENABLED=true mise run evaluate-llm` inside DevPod with the reverse tunnel active. The script makes three model requests using synthetic fixture documents and writes `.agent/llm-evaluation.json`: citation presence, lexical checks for expected facts, latency, model ID and token usage. Lexical checks are a small regression signal; they do not establish general factual accuracy. No tenant documents or prompts are written to the report.

The separate **Optional live LLM evaluation** workflow is manual and disabled until repository variable `METIS_LIVE_LLM_EVAL_ENABLED=true` is configured. It requires a trusted self-hosted runner labeled `metis-llm` that can reach the bridge, plus `GPTMOCK_BASE_URL` and `GPTMOCK_API_KEY` repository secrets. Dispatch it with `confirm_live_requests=true` and an advertised model ID; its artifact contains only the synthetic evaluation results. Runner setup and credentials are external configuration steps. Never run untrusted PR code on that runner.

## External knowledge

Knowledge supports adding a Microsoft 365 library and requesting synchronization. The worker imports changed supported files and removes deleted files from future retrieval, while preserving version history and source links. Periodic synchronization is opt-in. See [Microsoft 365 setup and limitations](docs/microsoft365.md) for source-bound credentials, organisation-wide library permissions, external Secret names, scheduling and verification status. Real Microsoft 365 access requires operator configuration; normal CI uses synthetic connector fixtures.

## Performance baseline

See [retrieval measurements and scaling decisions](docs/performance.md). `METIS_BENCHMARK_ENABLED=true mise run benchmark-retrieval` measures exact retrieval against an explicitly configured, isolated PostgreSQL database and cleans up its synthetic tenants. Current measurements do not justify caching, approximate vector indexes, worker autoscaling or distributed database components.
