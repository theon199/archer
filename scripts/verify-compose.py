#!/usr/bin/env python3
"""Exercise a disposable Compose deployment against the existing local model."""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68"


def verify(image):
    checks = {}
    project = "archer-verify-" + secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix="archer-compose-check-") as directory:
        root = Path(directory)
        for name in ("bridge", "data", "files"):
            (root / name).mkdir(mode=0o700)
        environment = dict(
            os.environ,
            ARCHER_IMAGE=image,
            ARCHER_UID=str(os.getuid()),
            ARCHER_GID=str(os.getgid()),
            ARCHER_BRIDGE_DIR=str(root / "bridge"),
            ARCHER_DATA_DIR=str(root / "data"),
            ARCHER_APPROVED_FILES_ROOT=str(root / "files"),
            ARCHER_EXPECTED_MODEL_DIGEST=DIGEST,
        )
        compose = ["docker", "compose", "-p", project, "-f", str(ROOT / "config/compose/compose.yaml")]
        relay = subprocess.Popen(
            ["socat", f"UNIX-LISTEN:{root / 'bridge/model.sock'},mode=0600,fork", "TCP:127.0.0.1:11434"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not (root / "bridge/model.sock").exists():
                if relay.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Owned model socket relay unavailable")
                time.sleep(0.05)
            # Provision a disposable owner inside the same image and data mount.
            password = secrets.token_urlsafe(24)
            env = dict(environment, SYNTHETIC_PASSWORD=password)
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network=none",
                    "--read-only",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--mount",
                    f"type=bind,src={root / 'data'},dst=/data",
                    "-e",
                    "ARCHER_DATA_DIR=/data",
                    "-e",
                    "SYNTHETIC_PASSWORD",
                    image,
                    "python",
                    "-c",
                    "import asyncio,os; from app.auth import provision_owner; asyncio.run(provision_owner('compose-test-owner',os.environ['SYNTHETIC_PASSWORD']))",
                ],
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
            )
            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o755)
            unsafe.chmod(0o755)
            rejected = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network=none",
                    "--read-only",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--mount",
                    f"type=bind,src={unsafe},dst=/bridge",
                    "--mount",
                    f"type=bind,src={root / 'data'},dst=/data",
                    "--mount",
                    f"type=bind,src={root / 'files'},dst=/approved,readonly",
                    image,
                    "python",
                    "-m",
                    "app.compose_guard",
                    "app",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            checks["unsafe_socket_directory_rejected"] = rejected.returncode != 0 and not (unsafe / "api.sock").exists()
            subprocess.run(
                [*compose, "up", "-d", "--pull", "never"],
                env=environment,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            transport = httpx.HTTPTransport(uds=str(root / "bridge/api.sock"))
            with httpx.Client(transport=transport, base_url="http://localhost", timeout=10) as client:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        ready = client.get("/api/ready")
                        if ready.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Compose readiness deadline")
                checks["ready_with_real_local_model_metadata"] = ready.json().get("status") == "ready"
                checks["frontend_served"] = client.get("/").status_code == 200
                checks["data_requires_authentication"] = client.get("/api/v1/conversations").status_code == 401
                login = client.post("/api/auth/login", json={"username": "compose-test-owner", "password": password})
                login.raise_for_status()
                client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
                response = client.post(
                    "/api/v1/runs",
                    json={"message": "Reply briefly with the word READY.", "client_operation_id": "compose-test"},
                )
                response.raise_for_status()
                run = response.json()
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    current = client.get(f"/api/v1/runs/{run['id']}").json()
                    if current["status"] in {"succeeded", "failed", "canceled", "needs_reconciliation"}:
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError("Compose model generation deadline")
                checks["real_local_model_run_succeeded"] = current["status"] == "succeeded" and bool(
                    current["output"].strip()
                )
                checks["idempotent_submission"] = (
                    client.post(
                        "/api/v1/runs",
                        json={"message": "Reply briefly with the word READY.", "client_operation_id": "compose-test"},
                    )
                    .json()
                    .get("id")
                    == run["id"]
                )
                listing = subprocess.check_output([*compose, "ps", "--format", "json"], env=environment, text=True)
                containers = [json.loads(line) for line in listing.splitlines() if line.strip()]
                checks["no_published_ports"] = len(containers) == 3 and all(
                    not item.get("Publishers") for item in containers
                )
                token = client.headers["Authorization"]
            subprocess.run(
                [*compose, "restart", "app", "worker"],
                env=environment,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
            )
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(root / "bridge/api.sock")),
                base_url="http://localhost",
                timeout=10,
            ) as client:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        ready = client.get("/api/ready")
                        if ready.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Compose restart readiness deadline")
                client.headers["Authorization"] = token
                restored = client.get(f"/api/v1/runs/{run['id']}")
                checks["session_and_run_survive_restart"] = (
                    restored.status_code == 200
                    and restored.json()["status"] == "succeeded"
                    and restored.json()["output"] == current["output"]
                )
            return {"passed": all(checks.values()), "checks": checks, "image": image}
        finally:
            subprocess.run(
                [*compose, "down", "--remove-orphans"],
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            relay.terminate()
            relay.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    try:
        with args.evidence.open("x") as output:
            try:
                result = verify(args.image)
            except Exception as error:  # noqa: BLE001 - Do not print provider output or private paths.
                result = {"passed": False, "error_type": type(error).__name__}
            result["checked_at"] = datetime.now(UTC).isoformat()
            result["scope"] = (
                "Disposable ARM Compose app, worker and model bridge; real local model, no browser or MCP connector."
            )
            json.dump(result, output, indent=2)
            output.write("\n")
        print(json.dumps(result))
        sys.exit(0 if result["passed"] else 1)
    except FileExistsError:
        print("Evidence path already exists", file=sys.stderr)
        sys.exit(2)
