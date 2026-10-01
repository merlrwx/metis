# Metis

Metis will let organisations upload internal knowledge and ask questions with citations to the original documents. The implementation plan is in [plans/plan1.md](plans/plan1.md).

This starting milestone implements the application and delivery foundation from Phase 0. It reuses [devops-app](https://github.com/merlrwx/devops-app): independent uv projects, FastAPI, Streamlit, mise, multi-stage non-root Docker images, Ruff/pre-commit, pytest coverage, Trivy, Release Please, GHCR, k3d and Flux setup tools.

The current milestone adds PostgreSQL, SQLAlchemy and Alembic migrations, plus tenant-associated organisation, source, and document metadata. The API exposes `GET /health`, `GET /api/info`, and organisation/source/document metadata endpoints. Authentication, binary upload, ingestion, retrieval and chat follow in later phases.

## Development with DevPod

```bash
devpod up . --id metis --ide none
devpod ssh metis
cd /workspaces/metis
mise exec -- ./scripts/verify
```

The devcontainer keeps the base project's Docker-in-Docker feature. Setup installs the mise tools and locked dependencies for all three uv projects. [DevPod also supports creating a workspace from a local folder or Git repository](https://devpod.sh/docs/developing-in-workspaces/create-a-workspace).

Start PostgreSQL for local API development:

```bash
docker compose up -d database
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
  uv run --locked --project src/backend alembic -c src/backend/alembic.ini upgrade head
DATABASE_URL=postgresql+psycopg://metis:metis-local-only@localhost:5432/metis \
  uv run --locked --project src/backend metis-api
```

Run Streamlit in another terminal with `uv run --locked --project src/frontend streamlit run src/frontend/app.py`. The API is on port 8000 (`/docs` for OpenAPI), and Streamlit is on port 8501. Set `BACKEND_URL` if the API runs elsewhere. DevPod forwards these ports.

Alternatively, `mise exec -- bash scripts/test-postgres` applies migrations and runs the backend integration tests against PostgreSQL. `docker compose up --build` starts PostgreSQL, applies Alembic migrations and starts both application services. The default database password is local-only; set `POSTGRES_PASSWORD` for a personal deployment and do not reuse it elsewhere.

## Verification

```bash
mise exec -- ./scripts/verify
mise exec -- pre-commit run --all-files
mise exec -- uv run --locked --project kubernetes python kubernetes/e2e_test.py
```

`verify` checks Ruff lint and formatting, backend and frontend tests with at least 80% coverage, dependency locks, Compose configuration and rendered Kubernetes manifests. Frontend tests run Streamlit's AppTest with mocked backend responses; normal CI never calls an LLM.

E2E builds both Docker images and runs the Metis services in a disposable `metis-cluster` k3d cluster. It checks API identity, removed study-tracker routes and frontend HTTP health. Successful runs delete the test cluster; failures retain it for inspection. E2E operates only on the local Docker-provider environment. See [kubernetes/README.md](kubernetes/README.md).

## Delivery and credentials

Backend and frontend checks, coverage, container builds and Trivy scans run on PRs and pushes to main. Kubernetes E2E also runs on PRs and main. Container release workflows publish `ghcr.io/merlrwx/metis-api` and `ghcr.io/merlrwx/metis-web` when `backend*` and `frontend*` tags are pushed.

Release Please retains separate backend/frontend releases, starting at 0.1.0. Set the `METIS_RELEASE_TOKEN` repository secret to a token allowed to create release PRs and tags, then set the repository variable `ENABLE_RELEASE` to `true`. A dedicated token is needed for release tags to trigger downstream image workflows. The new repository does not inherit secrets from devops-app.

GitOps updates are opt-in via repository variable `ENABLE_GITOPS=true`. Before enabling:

1. Provision the separate `merlrwx/metis-gitops` repo with Flux `clusters/dev`, `apps/dev` and `apps/prod` overlays using the Metis namespace and image names. This bootstrap does not create that repo or deploy to a shared cluster.
2. Add `GITOPS_DEPLOY_KEY`, a write-enabled SSH private key for that repo, and `METIS_RELEASE_TOKEN`, a token permitted to open its production promotion PRs.
3. Use the preserved `mise run setup-keys` and `mise run setup-cluster-gitops` tools only when ready to create the GitOps repo and bootstrap Flux. Keys stay in ignored `dev-keys/` and use Metis names.

The reusable workflow updates dev image tags and opens a prod promotion PR. GitOps and live Flux reconciliation remain unverified until that separate repo and credentials are configured. The existing devops-app GitOps environment is unchanged.

## Local LLM testing (later RAG phase)

The supplied plan uses LangChain `ChatOpenAI` with the existing GPTMock bridge. On the workstation:

```bash
kubectl -n hermes port-forward svc/chatmock 8000:8000
```

Use `GPTMOCK_BASE_URL=http://127.0.0.1:8000/v1`, `GPTMOCK_MODEL=gpt-6-luna`, placeholder API key `chatmock`, `use_responses_api=False`, a 120-second timeout and one retry. This port-forward occupies the same workstation port as the API; use port 8001 for the bridge if running both and update the base URL.

Inside Kubernetes, the planned URL is `http://chatmock.hermes.svc.cluster.local:8000/v1`. A DevPod has its own network namespace: its localhost is not the workstation. Reach a workstation port-forward through an explicit SSH reverse tunnel or a reachable host address. For example, from the workstation, after forwarding the bridge to port 8001:

```bash
devpod ssh metis -R 8001:127.0.0.1:8001
# Inside that session, use GPTMOCK_BASE_URL=http://127.0.0.1:8001/v1
```

LangChain and embeddings will be added in their planned phases. LangGraph is deferred until a real branching workflow requires it, as specified by the architectural plan. This milestone makes no live LLM calls and does not verify bridge availability.
