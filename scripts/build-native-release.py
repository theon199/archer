#!/usr/bin/env python3
"""Build a secret-free native release from the existing locked projects."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(destination, sqlite_runtime):
    sys.path.insert(0, str(ROOT / "backend"))
    from app.readiness import validate_runtime

    validate_runtime()
    from app.sqlite_runtime import runtime_identity

    identity = runtime_identity()
    sqlite_runtime = sqlite_runtime.resolve(strict=True)
    sqlite_manifest = json.loads((sqlite_runtime / "sqlite-runtime.json").read_text())
    if any(identity[key] != sqlite_manifest[key] for key in identity):
        raise ValueError("Build must run with the declared private SQLite runtime")
    library = sqlite_runtime / "lib/libsqlite3.so.0"
    if library.is_symlink() or hashlib.sha256(library.read_bytes()).hexdigest() != identity["library_sha256"]:
        raise ValueError("SQLite runtime library integrity mismatch")
    if destination.exists():
        raise FileExistsError("Release destination already exists")
    for command in [["pnpm", "install", "--frozen-lockfile"], ["pnpm", "build"]]:
        subprocess.run(command, cwd=ROOT / "frontend", check=True)
    with tempfile.TemporaryDirectory(prefix=".archer-release-", dir=destination.parent) as work:
        payload = Path(work) / "payload"
        (payload / "backend").mkdir(parents=True)
        for path in sorted((ROOT / "backend/app").rglob("*.py")):
            if path.is_symlink():
                raise ValueError("Source symlinks are not permitted")
            target = payload / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        for name in ["pyproject.toml", "uv.lock", "README.md"]:
            shutil.copyfile(ROOT / "backend" / name, payload / "backend" / name)
        shutil.copytree(ROOT / "frontend/dist", payload / "frontend/dist")
        (payload / "runtime/sqlite/lib").mkdir(parents=True)
        shutil.copyfile(library, payload / "runtime/sqlite/lib/libsqlite3.so.0")
        shutil.copyfile(sqlite_runtime / "sqlite-runtime.json", payload / "runtime/sqlite/sqlite-runtime.json")
        (payload / "bin").mkdir()
        shutil.copyfile(ROOT / "scripts/with-sqlite-runtime.py", payload / "bin/with-sqlite-runtime.py")
        files = {
            p.relative_to(payload).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(payload.rglob("*"))
            if p.is_file()
        }
        manifest = {
            "format": 1,
            "built_at": datetime.now(UTC).isoformat(),
            "python": sys.version.split()[0],
            "sqlite_runtime": identity,
            "architecture": platform.machine(),
            "node": subprocess.check_output(["node", "--version"], text=True).strip(),
            "pnpm": subprocess.check_output(["pnpm", "--version"], text=True).strip(),
            "uv": subprocess.check_output(["uv", "--version"], text=True).strip(),
            "files": files,
        }
        (payload / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # Reuse the tested Linux no-replace publication primitive; no host changes.
        sys.path.insert(0, str(ROOT / "backend"))
        from app.backup import publish_directory

        publish_directory(payload, destination)
    return {
        "destination_created": True,
        "files": len(files),
        "manifest_sha256": hashlib.sha256((destination / "release-manifest.json").read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--sqlite-runtime", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.destination, args.sqlite_runtime)))
