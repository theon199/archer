#!/usr/bin/env python3
"""Actual private SQLite identity/reproducibility and fail-closed CLI checks."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(runtime, repeat, evidence):
    checks = {}
    wrapper = ROOT / "scripts/with-sqlite-runtime.py"
    python = ROOT / "backend/.venv/bin/python"
    manifest = json.loads((runtime / "sqlite-runtime.json").read_text())
    duplicate = json.loads((repeat / "sqlite-runtime.json").read_text())
    command = [sys.executable, str(wrapper), "--runtime", str(runtime), "--"]
    probe = subprocess.run(
        [
            *command,
            str(python),
            "-c",
            "import json; from app.sqlite_runtime import runtime_identity; print(json.dumps(runtime_identity()))",
        ],
        cwd=ROOT / "backend",
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    identity = json.loads(probe.stdout)
    checks["actual_mapped_library_matches_manifest"] = all(identity[key] == manifest[key] for key in identity)
    checks["pinned_fixed_version"] = identity["version"] == "3.54.1"
    checks["repeat_build_identical_library_bytes"] = (
        hashlib.sha256((runtime / "lib/libsqlite3.so.0").read_bytes()).hexdigest()
        == hashlib.sha256((repeat / "lib/libsqlite3.so.0").read_bytes()).hexdigest()
        == manifest["library_sha256"]
        == duplicate["library_sha256"]
    )
    unwrapped_environment = dict(os.environ)
    unwrapped_environment.pop("LD_LIBRARY_PATH", None)
    with tempfile.TemporaryDirectory(prefix="archer-sqlite-verify-") as directory:
        work = Path(directory)
        marker = work / "should-not-exist"
        unwrapped_environment["ARCHER_DATA_DIR"] = str(work / "unused-data")
        rejected = subprocess.run(
            [str(python), "-m", "app.serve"],
            cwd=ROOT / "backend",
            check=False,
            env=unwrapped_environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        checks["unfixed_host_runtime_startup_rejected"] = (
            rejected.returncode != 0 and "WAL-reset fix" in rejected.stderr
        )
        checks["rejected_startup_creates_no_data"] = not (work / "unused-data").exists()
        altered = work / "altered"
        (altered / "lib").mkdir(parents=True)
        shutil.copyfile(runtime / "lib/libsqlite3.so.0", altered / "lib/libsqlite3.so.0")
        (altered / "sqlite-runtime.json").write_text(json.dumps(dict(manifest, architecture="wrong-architecture")))
        arguments = [
            sys.executable,
            str(wrapper),
            "--runtime",
            str(altered),
            "--",
            str(python),
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
            str(marker),
        ]
        wrong = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=10)
        checks["architecture_mismatch_stops_before_command"] = wrong.returncode != 0 and not marker.exists()
        (altered / "sqlite-runtime.json").write_text(json.dumps(manifest))
        with (altered / "lib/libsqlite3.so.0").open("r+b") as library:
            library.seek(100)
            original = library.read(1)
            library.seek(100)
            library.write(bytes([original[0] ^ 1]))
        tampered = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=10)
        checks["modified_library_stops_before_command"] = tampered.returncode != 0 and not marker.exists()
        # A mismatched release receipt must fail before API/worker/data creation.
        release = work / "release"
        shutil.copytree(ROOT / "backend/app", release / "backend/app", ignore=shutil.ignore_patterns("__pycache__"))
        (release / "release-manifest.json").write_text(
            json.dumps(
                {
                    "python": sys.version.split()[0],
                    "sqlite_runtime": dict(identity, source_id="incorrect-source"),
                    "architecture": manifest["architecture"],
                    "files": {},
                }
            )
        )
        environment = dict(unwrapped_environment, LD_LIBRARY_PATH=str(runtime / "lib"))
        mismatched = subprocess.run(
            [str(python), "-m", "app.serve"],
            cwd=release / "backend",
            check=False,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        checks["release_sqlite_identity_mismatch_rejected"] = (
            mismatched.returncode != 0 and "SQLite runtime differs" in mismatched.stderr
        )
        checks["mismatched_release_creates_no_data"] = not (work / "unused-data").exists()
    result = {
        "passed": all(checks.values()),
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "identity": identity,
        "scope": "Actual subprocesses, mapped shared-library bytes, two real official-source builds and startup refusals. No production data, model inference or host package changes.",
    }
    evidence.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--repeat", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(verify(args.runtime.resolve(), args.repeat.resolve(), args.evidence))
