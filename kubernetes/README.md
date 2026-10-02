# Metis Kubernetes development

The Kustomize base deploys a stateless API, worker and frontend with shared runtime configuration. It expects a `metis-runtime` Secret plus PostgreSQL and Redis services. The dev overlay supplies PostgreSQL with pgvector, Redis, a local-only Secret and a temporary shared object directory in namespace `metis`. It uses deterministic hashing embeddings; those values are for development only. The k3d overlay does not call an LLM. Configure `GPTMOCK_BASE_URL` to a reachable address before using chat there. The separate GitOps dev overlay targets the cluster-local ChatMock service.

The dev API and frontend services are `dev-backend` and `dev-frontend`, exposing ports 22112 and 22111. k3d assigns their `LoadBalancer` addresses. The base services are `ClusterIP` for ordinary cluster networking. The API and frontend have HTTP readiness and liveness probes; PostgreSQL and Redis have dependency-specific probes. All application containers have resource requests and memory limits.

Run the complete disposable-cluster test from the repository root inside the DevPod:

```bash
mise exec -- uv run --locked --project kubernetes python kubernetes/e2e_test.py
```

The test builds and imports both images, applies the dev overlay, checks the upload-to-index-to-search path and tenant isolation, scales API and worker independently, and verifies that a worker completes an in-flight job when Kubernetes terminates it. The worker runs Taskiq as PID 1 and has a 90-second termination grace period. Jobs are acknowledged only after execution; if Kubernetes must force-stop a worker, the next worker's startup wake-up recovers the unacknowledged Redis entry. The script deletes `metis-cluster` on success and keeps it on failure. Use `--skip-cluster-creation` to reuse it or `--no-cleanup` to retain it.

For interactive local Kubernetes development, `mise run k8s-setup-local` builds and deploys the application. `mise run k8s-setup-minimal` creates only the cluster. Render either Kustomize layer without applying it:

```bash
kubectl kustomize kubernetes/manifests/base
kubectl kustomize kubernetes/manifests/dev
```

The public development Secret contains disposable credentials only. Never reuse it. A production `metis-runtime` Secret must provide `DATABASE_URL`, `METIS_AUTH_SECRET_KEY`, and `METIS_BOOTSTRAP_TOKEN`; configure `GPTMOCK_API_KEY` and `EMBEDDING_API_KEY` when their providers require them. Production database URLs must point to PostgreSQL with pgvector. The production storage backend should be S3-compatible and use an external bucket and workload identity or separately managed credentials.

The GitOps deployment target is the private `merlrwx/metis-gitops` repository. It uses `apps/base` for the stateless app and `apps/dev` and `apps/prod` for environment-specific configuration and image references. Dev runs disposable PostgreSQL/Redis; prod expects operator-managed PostgreSQL with pgvector and a provisioned Redis endpoint. Provisioning the repo, Flux bootstrap and reconciliation are separate operator actions; the E2E test does not access shared or production clusters.
