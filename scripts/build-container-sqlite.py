#!/usr/bin/env python3
"""Build the pinned SQLite source with a pinned Debian ARM64 compiler image."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPILER = "gcc@sha256:1b17f91c2c0b0a696f9aa9686f2481fe756cc22c2eb4c517ee837e8f626a25b7"
PYTHON = "python@sha256:c3729439afc4ec3f3e159e0c86aeabfadac8030ef46838a0256383d0511c5f5e"


def build(destination):
    if platform.machine() != "aarch64" or destination.exists() or destination.is_symlink():
        raise ValueError("Requires ARM64 and a new destination")
    spec = importlib.util.spec_from_file_location("sqlite_build", ROOT / "scripts/build-sqlite-runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with urllib.request.urlopen(module.URL, timeout=30) as response:
        if response.geturl() != module.URL:
            raise ValueError("Unexpected source redirect")
        archive = response.read(4 * 1024 * 1024 + 1)
    if len(archive) > 4 * 1024 * 1024 or hashlib.sha3_256(archive).hexdigest() != module.ARCHIVE_SHA3:
        raise ValueError("SQLite archive checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        member = bundle.getinfo("sqlite-amalgamation-3540100/sqlite3.c")
        if member.file_size > 16 * 1024 * 1024:
            raise ValueError("SQLite source too large")
        source = bundle.read(member)
    if hashlib.sha3_256(source).hexdigest() != module.SOURCE_SHA3:
        raise ValueError("SQLite source checksum mismatch")
    destination = destination.absolute()
    with tempfile.TemporaryDirectory(prefix=".archer-container-sqlite-", dir=destination.parent) as work:
        work = Path(work)
        (work / "sqlite3.c").write_bytes(source)
        common = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={work},dst=/work",
            "--workdir",
            "/work",
        ]
        subprocess.run(
            [*common, COMPILER, "gcc", *module.FLAGS, "sqlite3.c", "-o", "libsqlite3.so.0", "-lpthread", "-ldl", "-lm"],
            check=True,
            timeout=180,
        )
        probe = json.loads(
            subprocess.check_output(
                [
                    *common,
                    "--env",
                    "LD_LIBRARY_PATH=/work",
                    PYTHON,
                    "python",
                    "-c",
                    "import json,sqlite3; c=sqlite3.connect(':memory:'); print(json.dumps({'version':sqlite3.sqlite_version,'source_id':c.execute('select sqlite_source_id()').fetchone()[0],'threadsafety':sqlite3.threadsafety}))",
                ],
                text=True,
                timeout=20,
            )
        )
        if probe != {"version": module.VERSION, "source_id": module.SOURCE_ID, "threadsafety": 3}:
            raise ValueError("Container runtime failed SQLite identity check")
        payload = work / "payload"
        (payload / "lib").mkdir(parents=True)
        library = payload / "lib/libsqlite3.so.0"
        (work / "libsqlite3.so.0").rename(library)
        manifest = {
            "version": module.VERSION,
            "source_id": module.SOURCE_ID,
            "architecture": platform.machine(),
            "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
            "archive_sha3_256": module.ARCHIVE_SHA3,
            "source_sha3_256": module.SOURCE_SHA3,
            "compiler_image": COMPILER,
            "python_image": PYTHON,
            "built_at": datetime.now(UTC).isoformat(),
            "flags": module.FLAGS,
        }
        (payload / "sqlite-runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # Atomic publication without replacing an existing runtime.
        sys.path.insert(0, str(ROOT / "backend"))
        from app.backup import publish_directory

        publish_directory(payload, destination)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    print(json.dumps(build(parser.parse_args().destination), sort_keys=True))
