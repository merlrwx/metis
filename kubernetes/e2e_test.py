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
SERVICES = ("dev-backend", "dev-worker", "dev-frontend")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("e2e-tests")


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


def api(base_url, path, method="GET", payload=None, expected_status=200, headers=None):
    response = requests.request(
        method, f"{base_url}{path}", json=payload, headers=headers, timeout=5
    )
    if response.status_code != expected_status:
        raise AssertionError(
            f"{method} {path}: expected HTTP {expected_status}, got "
            f"{response.status_code}: {response.text}"
        )
    return response.json()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def test_backend(base_url):
    require(api(base_url, "/health") == {"status": "ok"}, "Backend health check failed")
    info = api(base_url, "/api/info")
    require(info["name"] == "Metis", "Wrong backend application")
    require(info["stage"] == "async-processing", "Wrong application stage")
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
