#!/usr/bin/env python3
"""Actual native release startup/restart drill; synthetic owner, real model metadata."""

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def verify(release):
    checks = {}
    with tempfile.TemporaryDirectory(prefix="archer-native-drill-") as directory:
        runtime = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        environment = dict(
            os.environ,
            ARCHER_DATA_DIR=str(runtime / "data"),
            ARCHER_APPROVED_FILES_ROOT=str(runtime / "files"),
            ARCHER_STATIC_DIR=str(release / "frontend/dist"),
            ARCHER_HOST="127.0.0.1",
            ARCHER_PORT=str(port),
            ARCHER_MODEL_PROVIDER="local-ollama",
            ARCHER_OLLAMA_BASE_URL="http://127.0.0.1:11434",
            ARCHER_OLLAMA_MODEL="gemma4:26b",
            ARCHER_EXPECTED_MODEL_DIGEST="08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68",
        )
        python = str(release / "backend/.venv/bin/python")
        password = secrets.token_urlsafe(24)
        subprocess.run(
            [
                python,
                "-c",
                "import asyncio,sys; from app.auth import provision_owner; asyncio.run(provision_owner('drill',sys.stdin.read()))",
            ],
            input=password,
            text=True,
            env=environment,
            cwd=release / "backend",
            check=True,
        )
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=3
        ) as client:
            token = None
            for attempt in range(2):
                with (runtime / f"startup-{attempt}.log").open("w") as log:
                    process = subprocess.Popen(
                        [python, "-m", "app.serve"],
                        cwd=release / "backend",
                        env=environment,
                        stdout=log,
                        stderr=log,
                    )
                    try:
                        ready = None
                        for _ in range(80):
                            if process.poll() is not None:
                                raise RuntimeError(
                                    "Launcher exited: "
                                    + (runtime / f"startup-{attempt}.log").read_text()[
                                        -1500:
                                    ]
                                )
                            try:
                                ready = client.get("/api/ready")
                                if ready.status_code == 200:
                                    break
                            except httpx.TransportError:
                                pass
                            time.sleep(0.1)
                        checks[f"ready_after_start_{attempt}"] = bool(
                            ready is not None
                            and ready.status_code == 200
                            and all(
                                ready.json()[k]
                                for k in ["database", "worker", "local_model"]
                            )
                        )
                        assert checks[f"ready_after_start_{attempt}"]
                        if attempt == 0:
                            page = client.get("/")
                            checks["built_frontend_served"] = (
                                page.status_code == 200 and 'id="root"' in page.text
                            )
                            asset = re.search(r'src="([^"]+\.js)"', page.text).group(1)
                            checks["built_javascript_served"] = (
                                client.get(asset).status_code == 200
                            )
                            response = client.get("/api/notes")
                            checks["owner_auth_required"] = response.status_code == 401
                            checks["api_not_cached"] = (
                                response.headers.get("cache-control") == "no-store"
                            )
                            login = client.post(
                                "/api/auth/login",
                                json={"username": "drill", "password": password},
                            )
                            login.raise_for_status()
                            token = login.json()["access_token"]
                        else:
                            response = client.get(
                                "/api/v1/conversations",
                                headers={"Authorization": "Bearer " + token},
                            )
                            checks["owner_session_durable_after_restart"] = (
                                response.status_code == 200
                            )
                            children = (
                                Path(f"/proc/{process.pid}/task/{process.pid}/children")
                                .read_text()
                                .split()
                            )
                            worker = next(
                                int(pid)
                                for pid in children
                                if b"app.runs.worker"
                                in Path(f"/proc/{pid}/cmdline").read_bytes()
                            )
                            os.kill(worker, signal.SIGTERM)
                            process.wait(timeout=15)
                            checks["worker_exit_stops_launcher"] = (
                                process.returncode != 0
                            )

                    finally:
                        process.terminate()
                        process.wait(timeout=15)
                    checks[f"launcher_expected_exit_{attempt}"] = (
                        process.returncode == (0 if attempt == 0 else 1)
                    )
                    try:
                        client.get("/api/ready")
                        checks[f"owned_api_stopped_{attempt}"] = False
                    except httpx.TransportError:
                        checks[f"owned_api_stopped_{attempt}"] = True
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": all(checks.values()),
        "checks": checks,
        "manifest_sha256": hashlib.sha256(
            (release / "release-manifest.json").read_bytes()
        ).hexdigest(),
        "scope": "Real HTTP, fresh locked release venv, synthetic owner SQLite, existing local Ollama metadata/digest. No inference or browser execution; no production data.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.release.resolve())
    args.evidence.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
