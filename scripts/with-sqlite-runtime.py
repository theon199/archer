#!/usr/bin/env python3
"""Run a command with exactly the reviewed private SQLite library directory."""

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    manifest = json.loads((runtime / "sqlite-runtime.json").read_text())
    library = runtime / "lib/libsqlite3.so.0"
    if (
        library.is_symlink()
        or manifest["architecture"] != platform.machine()
        or hashlib.sha256(library.read_bytes()).hexdigest() != manifest["library_sha256"]
    ):
        raise SystemExit("SQLite runtime integrity/architecture mismatch")
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("Provide a command after --")
    environment = dict(os.environ, LD_LIBRARY_PATH=str(runtime / "lib"))
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
