#!/usr/bin/env python3
"""Build a local ARM application image from a minimal secret-free context."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(runtime: Path, tag: str):
    runtime = runtime.resolve(strict=True)
    manifest = json.loads((runtime / "sqlite-runtime.json").read_text())
    library = runtime / "lib/libsqlite3.so.0"
    if library.is_symlink() or hashlib.sha256(library.read_bytes()).hexdigest() != manifest["library_sha256"]:
        raise ValueError("SQLite library mismatch")
    if manifest.get("python_image") != "python@sha256:c3729439afc4ec3f3e159e0c86aeabfadac8030ef46838a0256383d0511c5f5e":
        raise ValueError("Runtime was not tested with the pinned Python image")
    with tempfile.TemporaryDirectory(prefix="archer-compose-build-") as directory:
        context = Path(directory)
        for command in (["pnpm", "install", "--frozen-lockfile"], ["pnpm", "build"]):
            subprocess.run(command, cwd=ROOT / "frontend", check=True, timeout=300)
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(context / "requirements.txt"),
            ],
            cwd=ROOT / "backend",
            check=True,
            timeout=60,
            stdout=subprocess.DEVNULL,
        )
        for source in (ROOT / "backend/app", ROOT / "frontend/dist", runtime):
            if any(path.is_symlink() for path in source.rglob("*")):
                raise ValueError("Build context symlinks are not permitted")
        shutil.copytree(ROOT / "backend/app", context / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "frontend/dist", context / "dist")
        shutil.copytree(runtime, context / "sqlite")
        shutil.copyfile(ROOT / "config/compose/Dockerfile", context / "Dockerfile")
        for path in context.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        subprocess.run(
            ["docker", "build", "--platform", "linux/arm64", "--tag", tag, str(context)], check=True, timeout=900
        )
    image_id = subprocess.check_output(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True).strip()
    return {"image": tag, "image_id": image_id, "sqlite_sha256": manifest["library_sha256"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-runtime", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.sqlite_runtime, args.tag), sort_keys=True))
