import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
KUBERNETES_DIR = Path(__file__).resolve().parent
CLUSTER_NAME = "metis-cluster"
NAMESPACE = "metis"
OBJECT_STORAGE_PATH = Path("/tmp/metis-k3d-objects")
SERVICES = ("dev-backend", "dev-worker", "dev-frontend")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("e2e-tests")
AUTH_HEADERS = {}


def run(*command, cwd=ROOT_DIR, check=True, capture_output=False, env=None):
    logger.info("Running: %s", " ".join(map(str, command)))
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        capture_output=capture_output,
        text=True,
        env=env,
    )


def setup_cluster(skip_cluster_creation):
    OBJECT_STORAGE_PATH.mkdir(parents=True, exist_ok=True)
    if skip_cluster_creation:
        run("kubectl", "config", "use-context", f"k3d-{CLUSTER_NAME}")
        return

    result = run("k3d", "cluster", "list", check=False, capture_output=True)
    clusters = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
    if CLUSTER_NAME in clusters:
        run("k3d", "cluster", "delete", CLUSTER_NAME)

    run(
        "k3d",
        "cluster",
        "create",
        "--config",
        KUBERNETES_DIR / "k3d-config.yaml",
    )
    run("kubectl", "config", "use-context", f"k3d-{CLUSTER_NAME}")
    run("kubectl", "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=60s")


def build_and_load_images():
    for app in ("backend", "frontend"):
        directory = ROOT_DIR / "src" / app
        with tempfile.TemporaryDirectory(prefix="metis-docker-") as docker_config:
            docker_env = {**os.environ, "DOCKER_CONFIG": docker_config}
            run(
                "docker",
                "build",
                "--tag",
                f"{app}:dev",
                "--file",
                directory / "Dockerfile",
                directory,
                env=docker_env,
            )
        run(
            "k3d",
            "image",
            "import",
            f"{app}:dev",
            "--cluster",
            CLUSTER_NAME,
            "--mode",
            "direct",
        )


def service_url(name):
    for attempt in range(30):
        result = run(
            "kubectl",
            "get",
            "service",
            name,
            "--namespace",
            NAMESPACE,
            "--output",
            "json",
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            service = json.loads(result.stdout)
            ingress = (
                service.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            )
            ports = service.get("spec", {}).get("ports", [])
            if ingress and ports:
                host = ingress[0].get("ip") or ingress[0].get("hostname")
                if host:
                    return f"http://{host}:{ports[0]['port']}"

        logger.info("Waiting for %s load balancer (%d/30)", name, attempt + 1)
        time.sleep(2)

    raise RuntimeError(f"No load balancer address assigned to {name}")


def deploy_application():
    run("kubectl", "apply", "-k", KUBERNETES_DIR / "manifests" / "dev")
    run(
        "kubectl",
        "rollout",
        "status",
        "deployment/dev-postgres",
        "--namespace",
        NAMESPACE,
        "--timeout=120s",
    )
    run(
        "kubectl",
        "rollout",
        "status",
        "deployment/dev-redis",
        "--namespace",
        NAMESPACE,
        "--timeout=120s",
    )
    for name in SERVICES:
        run(
            "kubectl",
            "rollout",
            "status",
            f"deployment/{name}",
            "--namespace",
            NAMESPACE,
            "--timeout=120s",
        )
    return service_url("dev-backend"), service_url("dev-frontend")


def wait_for_service(url):
    for attempt in range(20):
        try:
            if requests.get(url, timeout=5).status_code == 200:
                return
        except requests.RequestException:
            pass

        logger.info("Waiting for %s (%d/20)", url, attempt + 1)
        time.sleep(3)

    raise RuntimeError(f"Service is unavailable: {url}")


def wait_for_job(base_url, organisation_id, job_id, expected_status):
    job_url = f"/api/organisations/{organisation_id}/jobs/{job_id}"
    for _ in range(60):
        job = api(base_url, job_url)
        if job["status"] == expected_status:
            return job
        time.sleep(0.5)
    raise AssertionError(
        f"Job {job_id} did not reach {expected_status}; last status was {job['status']}"
    )


def wait_for_deployment(name, replicas):
    run(
        "kubectl",
        "rollout",
        "status",
        f"deployment/{name}",
        "--namespace",
        NAMESPACE,
        "--timeout=120s",
    )
    result = run(
        "kubectl",
        "get",
        "deployment",
        name,
        "--namespace",
        NAMESPACE,
        "--output",
        "json",
        capture_output=True,
    )
    deployment = json.loads(result.stdout)
    require(
        deployment["status"].get("readyReplicas", 0) == replicas,
        f"{name} did not have {replicas} ready replicas",
    )


def test_horizontal_scaling(base_url, organisation_id):
    run("kubectl", "scale", "deployment/dev-backend", "--replicas=2", "-n", NAMESPACE)
    wait_for_deployment("dev-backend", 2)
    wait_for_service(f"{base_url}/health")
    require(
        api(base_url, "/health") == {"status": "ok"}, "Scaled API failed health check"
    )
    run("kubectl", "scale", "deployment/dev-backend", "--replicas=1", "-n", NAMESPACE)
    wait_for_deployment("dev-backend", 1)

    run("kubectl", "scale", "deployment/dev-worker", "--replicas=2", "-n", NAMESPACE)
    wait_for_deployment("dev-worker", 2)
    job = api(
        base_url,
        "/api/jobs/test",
        "POST",
        {"organisation_id": organisation_id},
        202,
        {"Idempotency-Key": "metis-horizontal-worker-scale"},
    )
    completed = wait_for_job(base_url, organisation_id, job["id"], "completed")
    require(completed["attempts"] == 1, "Scaled workers processed a job more than once")
    run("kubectl", "scale", "deployment/dev-worker", "--replicas=1", "-n", NAMESPACE)
    wait_for_deployment("dev-worker", 1)


def test_worker_graceful_shutdown(base_url, organisation_id):
    worker = "deployment/dev-worker"
    # Deployment readiness excludes terminating pods, which can still consume
    # jobs with the old delay. Drain them before starting the delayed worker.
    run("kubectl", "scale", worker, "--replicas=0", "--namespace", NAMESPACE)
    run(
        "kubectl",
        "wait",
        "--for=delete",
        "pod",
        "--selector=component=worker",
        "--namespace",
        NAMESPACE,
        "--timeout=120s",
    )
    run(
        "kubectl",
        "set",
        "env",
        worker,
        "METIS_TEST_JOB_DELAY_SECONDS=6",
        "--namespace",
        NAMESPACE,
    )
    try:
        run("kubectl", "scale", worker, "--replicas=1", "--namespace", NAMESPACE)
        wait_for_deployment("dev-worker", 1)
        job = api(
            base_url,
            "/api/jobs/test",
            "POST",
            {"organisation_id": organisation_id},
            202,
            {"Idempotency-Key": "metis-graceful-worker-shutdown"},
        )
        processing = wait_for_job(base_url, organisation_id, job["id"], "processing")
        require(processing["attempts"] == 1, "Graceful shutdown job did not start once")
        run("kubectl", "scale", worker, "--replicas=0", "--namespace", NAMESPACE)
        completed = wait_for_job(base_url, organisation_id, job["id"], "completed")
        require(
            completed["attempts"] == 1,
            "Worker termination did not finish the in-flight job exactly once",
        )
    finally:
        run("kubectl", "scale", worker, "--replicas=1", "--namespace", NAMESPACE)
        wait_for_deployment("dev-worker", 1)
        run(
            "kubectl",
            "set",
            "env",
            worker,
            "METIS_TEST_JOB_DELAY_SECONDS-",
            "--namespace",
            NAMESPACE,
        )
        wait_for_deployment("dev-worker", 1)


def api(base_url, path, method="GET", payload=None, expected_status=200, headers=None):
    response = requests.request(
        method,
        f"{base_url}{path}",
        json=payload,
        headers={**AUTH_HEADERS, **(headers or {})},
        timeout=5,
    )
    if response.status_code != expected_status:
        raise AssertionError(
            f"{method} {path}: expected HTTP {expected_status}, got "
            f"{response.status_code}: {response.text}"
        )
    return response.json()


def upload_file(base_url, path, filename, content, mime_type, headers=None):
    response = requests.post(
        f"{base_url}{path}",
        files={"file": (filename, content, mime_type)},
        headers={**AUTH_HEADERS, **(headers or {})},
        timeout=15,
    )
    if response.status_code != 202:
        raise AssertionError(
            f"POST {path}: expected HTTP 202, got "
            f"{response.status_code}: {response.text}"
        )
    return response.json()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def test_backend(base_url):
    registered = api(
        base_url,
        "/api/auth/register",
        "POST",
        {
            "email": "e2e-owner@example.test",
            "name": "E2E Owner",
            "password": "e2e-test-password",
        },
        201,
    )
    token_response = requests.post(
        f"{base_url}/api/auth/token",
        data={"username": registered["email"], "password": "e2e-test-password"},
        timeout=5,
    )
    require(token_response.status_code == 200, "Bearer token login failed")
    AUTH_HEADERS["Authorization"] = f"Bearer {token_response.json()['access_token']}"
    api(
        base_url,
        "/api/organisations",
        "POST",
        {"name": "Unauthenticated"},
        401,
        {"Authorization": ""},
    )
    require(api(base_url, "/health") == {"status": "ok"}, "Backend health check failed")
    info = api(base_url, "/api/info")
    require(info["name"] == "Metis", "Wrong backend application")
    require(info["stage"] == "grounded-chat", "Wrong application stage")
    api(base_url, "/api/timer", expected_status=404)
    api(base_url, "/api/sessions", expected_status=404)
    organisation = api(
        base_url, "/api/organisations", "POST", {"name": "E2E Clinic"}, 201
    )
    source = api(
        base_url,
        f"/api/organisations/{organisation['id']}/sources",
        "POST",
        {"name": "Uploaded documents"},
        201,
    )
    document = api(
        base_url,
        f"/api/organisations/{organisation['id']}/documents",
        "POST",
        {"title": "Known policy", "source_id": source["id"]},
        201,
    )
    documents = api(base_url, f"/api/organisations/{organisation['id']}/documents")
    require(documents[0]["id"] == document["id"], "Document metadata did not persist")
    upload = upload_file(
        base_url,
        f"/api/organisations/{organisation['id']}/documents/upload",
        "policy.md",
        b"# Clinic policy\n\nRecord medication incidents promptly.",
        "text/markdown",
    )
    job_url = f"/api/organisations/{organisation['id']}/jobs/{upload['job']['id']}"
    for _ in range(30):
        job = api(base_url, job_url)
        if job["status"] == "indexed":
            break
        time.sleep(1)
    require(job["status"] == "indexed", "Uploaded document was not indexed")
    uploaded_document = api(
        base_url,
        f"/api/organisations/{organisation['id']}/documents/{upload['document']['id']}",
    )
    require(
        uploaded_document["ingestion_status"] == "indexed",
        "Document API did not expose indexed status",
    )
    results = api(
        base_url,
        f"/api/organisations/{organisation['id']}/search",
        "POST",
        {
            "query": "record medication incidents promptly",
            "source_id": upload["document"]["source_id"],
            "document_id": upload["document"]["id"],
            "limit": 1,
        },
    )
    require(
        results["results"]
        and results["results"][0]["document_id"] == upload["document"]["id"],
        "Vector search did not return the uploaded policy",
    )
    headers = {"Idempotency-Key": "metis-e2e-job"}
    job = api(
        base_url,
        "/api/jobs/test",
        "POST",
        {"organisation_id": organisation["id"]},
        202,
        headers,
    )
    job_url = f"/api/organisations/{organisation['id']}/jobs/{job['id']}"
    for _ in range(30):
        job = api(base_url, job_url)
        if job["status"] == "completed":
            break
        time.sleep(1)
    require(job["status"] == "completed", "Background job did not complete")
    duplicate = api(
        base_url,
        "/api/jobs/test",
        "POST",
        {"organisation_id": organisation["id"]},
        202,
        headers,
    )
    require(duplicate["id"] == job["id"], "Idempotent request created another job")
    require(job["attempts"] == 1, "Job was processed more than once")

    test_horizontal_scaling(base_url, organisation["id"])
    test_worker_graceful_shutdown(base_url, organisation["id"])
    run(
        "kubectl",
        "create",
        "job",
        "--from=cronjob/dev-reconcile-jobs",
        "dev-reconcile-e2e",
        "--namespace",
        NAMESPACE,
    )
    run(
        "kubectl",
        "wait",
        "--for=condition=complete",
        "job/dev-reconcile-e2e",
        "--namespace",
        NAMESPACE,
        "--timeout=120s",
    )
    run(
        "kubectl",
        "create",
        "job",
        "--from=cronjob/dev-sync-sources",
        "dev-sync-sources-e2e",
        "--namespace",
        NAMESPACE,
    )
    run(
        "kubectl",
        "wait",
        "--for=condition=complete",
        "job/dev-sync-sources-e2e",
        "--namespace",
        NAMESPACE,
        "--timeout=120s",
    )
    metrics = requests.get(f"{base_url}/metrics", timeout=10)
    require(metrics.status_code == 200, "API metrics unavailable")
    require("metis_queue_depth" in metrics.text, "Queue metrics missing")
    require(
        "metis_retrieval_duration_seconds" in metrics.text, "Retrieval metrics missing"
    )

    requests.post(
        f"{base_url}/api/auth/register",
        json={
            "email": "e2e-tenant-b@example.test",
            "name": "E2E Tenant B",
            "password": "e2e-test-password",
        },
        timeout=5,
    ).raise_for_status()
    tenant_b_token = requests.post(
        f"{base_url}/api/auth/token",
        data={
            "username": "e2e-tenant-b@example.test",
            "password": "e2e-test-password",
        },
        timeout=5,
    )
    require(tenant_b_token.status_code == 200, "Second tenant login failed")
    AUTH_HEADERS["Authorization"] = f"Bearer {tenant_b_token.json()['access_token']}"
    tenant_b = api(
        base_url, "/api/organisations", "POST", {"name": "E2E Tenant B"}, 201
    )
    isolated_results = api(
        base_url,
        f"/api/organisations/{tenant_b['id']}/search",
        "POST",
        {"query": "Record medication incidents promptly."},
    )
    require(isolated_results["results"] == [], "Tenant B retrieved Tenant A's document")
    hidden_document = api(
        base_url,
        f"/api/organisations/{organisation['id']}/documents/{upload['document']['id']}",
        expected_status=404,
    )
    require(bool(hidden_document), "Cross-tenant document lookup failed unexpectedly")


def test_frontend(base_url):
    health = requests.get(f"{base_url}/_stcore/health", timeout=5)
    require(
        health.status_code == 200, f"Frontend health returned HTTP {health.status_code}"
    )

    response = requests.get(base_url, timeout=5)
    require(
        response.status_code == 200, f"Frontend returned HTTP {response.status_code}"
    )
    require(
        response.headers.get("content-type", "").startswith("text/html"),
        "Frontend did not return an HTML page",
    )


def cleanup(skip_cluster_creation):
    if skip_cluster_creation:
        run("kubectl", "delete", "namespace", NAMESPACE, check=False)
    else:
        run("k3d", "cluster", "delete", CLUSTER_NAME, check=False)


def main():
    parser = argparse.ArgumentParser(
        description="Run end-to-end tests for the Metis application in k3d"
    )
    parser.add_argument("--skip-cluster-creation", action="store_true")
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args()

    missing = [tool for tool in ("docker", "kubectl", "k3d") if not shutil.which(tool)]
    if missing:
        parser.error(f"Required tools not found: {', '.join(missing)}")

    success = False
    try:
        setup_cluster(args.skip_cluster_creation)
        build_and_load_images()
        backend_url, frontend_url = deploy_application()
        wait_for_service(f"{backend_url}/health")
        wait_for_service(f"{frontend_url}/_stcore/health")
        test_backend(backend_url)
        test_frontend(frontend_url)
        success = True
        logger.info("End-to-end checks passed")
    except Exception:
        logger.exception("End-to-end checks failed")
    finally:
        if success and not args.no_cleanup:
            cleanup(args.skip_cluster_creation)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
