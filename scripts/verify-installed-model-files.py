#!/usr/bin/env python3
"""Read-only SHA-256 verification of this host's observed Ollama model files.

Produces checksums and executable identity only; never reads environment variables,
prompts, conversations, or credentials. Intended for a single owner-run sudo call.
"""

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path("/usr/share/ollama/.ollama/models")
MANIFEST = ROOT / "manifests/registry.ollama.ai/library/gemma4/26b"
EXPECTED_MANIFEST = "08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68"
BLOBS = (
    "41926ed5f1403cf5add23b0684992805ea6f97253096132e769e65646b8cef9d",
    "6326fb9f5e487aa8dcdd313a091e3c67724cb2a666ec3b7d2895b5b26d93ed1b",
    "dfd98d2734212d0c128e128e5d88edac764aaa4f4af2f4c196941f2354aab8da",
)


def digest(path, *, nofollow=True):
    fd = os.open(path, os.O_RDONLY | (os.O_NOFOLLOW if nofollow else 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Expected a regular file")
        value = hashlib.sha256()
        while chunk := stream.read(4 * 1024 * 1024):
            value.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("File changed while hashing")
        return {"sha256": value.hexdigest(), "bytes": after.st_size}


def main():
    manifest = digest(MANIFEST)
    blobs = {expected: digest(ROOT / "blobs" / ("sha256-" + expected)) for expected in BLOBS}
    pid = int(
        subprocess.check_output(
            ["systemctl", "show", "ollama.service", "--value", "--property=MainPID"], text=True
        ).strip()
    )
    if pid <= 1:
        raise ValueError("Ollama service is not running")
    executable = digest(Path(f"/proc/{pid}/exe"), nofollow=False)
    current_pid = int(
        subprocess.check_output(
            ["systemctl", "show", "ollama.service", "--value", "--property=MainPID"], text=True
        ).strip()
    )
    if current_pid != pid:
        raise ValueError("Ollama service changed while hashing")
    return {
        "passed": manifest["sha256"] == EXPECTED_MANIFEST and all(blobs[name]["sha256"] == name for name in BLOBS),
        "manifest": manifest,
        "manifest_matches_observed_digest": manifest["sha256"] == EXPECTED_MANIFEST,
        "blobs": blobs,
        "all_blob_names_match_hashes": all(blobs[name]["sha256"] == name for name in BLOBS),
        "serving_executable": executable,
        "serving_pid": pid,
        "scope": "Read-only hashes of previously observed local files and the running Ollama executable; not a publisher signature or complete runtime provenance.",
    }


if __name__ == "__main__":
    try:
        result = main()
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(0 if result["passed"] else 1)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None
