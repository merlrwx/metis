# Metis Kubernetes development

The base and dev Kustomize overlays deploy one API, one worker, and one frontend in the `metis` namespace, with PostgreSQL and Redis for development. Dev services are named `dev-backend` and `dev-frontend`, exposing ports 22112 and 22111 respectively. API and frontend health probes cover process readiness/liveness.

From the repository root inside the DevPod:

```bash
mise exec -- uv run --locked --project kubernetes python kubernetes/e2e_test.py
```

The script creates `metis-cluster`, builds/imports local images, applies the dev overlay, checks both services and deletes the cluster on success. A pre-existing cluster with that name is replaced; use `--skip-cluster-creation` to reuse it. `--no-cleanup` retains a successful test cluster.

For interactive local development, `mise run k8s-setup-local` builds and deploys the same application. `mise run k8s-setup-minimal` creates just the cluster.

GitOps tools target the separate `merlrwx/metis-gitops` repository and require its credentials. They create resources and bootstrap Flux only when invoked. See the root README for setup prerequisites; they are not part of normal verification.

The dev overlay shares a temporary host directory mounted into each k3d node for uploaded objects. That path is only for the disposable local environment; use the S3-compatible backend and deployment-specific credentials for a multi-node deployment. PostgreSQL/pgvector retrieval and production storage provisioning remain in later phases.
