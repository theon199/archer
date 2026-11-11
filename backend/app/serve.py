"""Foreground loopback app+worker launcher; owns and stops only its children."""

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    os.umask(0o077)
    from app.config import settings
    from app.readiness import validate_runtime

    validate_runtime()
    root = Path(__file__).resolve().parents[2]
    manifest_path = root / "release-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest["python"] != sys.version.split()[0]:
            raise SystemExit("Python version differs from the tested release runtime")
        from app.sqlite_runtime import runtime_identity

        if manifest.get("sqlite_runtime") != runtime_identity():
            raise SystemExit("SQLite runtime differs from the tested release runtime; rebuild older manifests")
        if manifest["architecture"] != platform.machine():
            raise SystemExit("Architecture differs from the tested release runtime")
        for name, expected in manifest["files"].items():
            path = (root / name).resolve()
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise SystemExit("Release file verification failed")
    if settings.static_dir is None:
        raise SystemExit("Set ARCHER_STATIC_DIR to the built frontend before deployment")
    environment = dict(os.environ, ARCHER_ENV="production")
    children = []
    stopped = False

    def stop(signum=None, frame=None):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    failed = False
    try:
        children.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    settings.host,
                    "--port",
                    str(settings.port),
                    "--no-access-log",
                ],
                env=environment,
            )
        )
        children.append(subprocess.Popen([sys.executable, "-m", "app.runs.worker"], env=environment))
        while not stopped:
            if any(child.poll() is not None for child in children):
                failed = True
                break
            time.sleep(0.2)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    if failed:
        raise SystemExit("An Archer child exited; both owned processes were stopped")


if __name__ == "__main__":
    main()
