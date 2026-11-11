#!/usr/bin/env python3
"""Build the reviewed official time connector from existing uv.lock hashes."""

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(context, evidence):
    context = context.resolve()
    context.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "config/mcp-time/Dockerfile", context / "Dockerfile")
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--extra",
            "time-mcp",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(context / "requirements.txt"),
        ],
        cwd=ROOT / "backend",
        stdout=subprocess.DEVNULL,
        check=True,
        timeout=30,
    )
    subprocess.run(
        [
            "docker",
            "build",
            "--network=default",
            "--tag",
            "archer-mcp-time:2026.10.19",
            "--iidfile",
            str(context / "image.id"),
            str(context),
        ],
        check=True,
        timeout=180,
    )
    image = (context / "image.id").read_text().strip()
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True, timeout=10))[0]
    if (
        info["Architecture"] != "arm64"
        or info["Config"]["Labels"].get("archer.connector") != "official-mcp-time-2026.10.19"
    ):
        raise ValueError("Unexpected connector image identity")
    result = {
        "image": image,
        "architecture": info["Architecture"],
        "image_bytes": info["Size"],
        "base_arm64_manifest": "sha256:c3729439afc4ec3f3e159e0c86aeabfadac8030ef46838a0256383d0511c5f5e",
        "python_version": "3.12.15",
        "mcp_server_time": "2026.10.19",
        "server_wheel_sha256": "5c099a001c6ca78260bda419f2e9b2aabee1adc4fe6ca56d8959871ad8da58a1",
        "requirements_sha256": hashlib.sha256((context / "requirements.txt").read_bytes()).hexdigest(),
        "dockerfile_sha256": hashlib.sha256((context / "Dockerfile").read_bytes()).hexdigest(),
        "uv_lock_sha256": hashlib.sha256((ROOT / "backend/uv.lock").read_bytes()).hexdigest(),
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Private local image build only; no integration success implied. Runtime must use reviewed image digest and existing fixed sandbox.",
    }
    evidence.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=ROOT / "build/mcp-time")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    build(args.context, args.evidence)
