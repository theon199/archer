"""Fail closed before starting a Compose process with non-private mounts."""

import os
import stat
import sys
from pathlib import Path


def main():
    for name in ("/bridge", "/data", "/approved"):
        directory = Path(name).lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise SystemExit(f"{name} must be an owned private directory")
    if sys.argv[1:] == ["app"]:
        os.execvp("python", ["python", "-m", "uvicorn", "app.main:app", "--uds", "/bridge/api.sock", "--no-access-log"])
    if sys.argv[1:] == ["worker"]:
        os.execvp("python", ["python", "-m", "app.runs.worker"])
    raise SystemExit("Expected app or worker")


if __name__ == "__main__":
    main()
