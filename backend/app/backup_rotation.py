"""Create a local encrypted backup and prune this tool's expired archives."""

import argparse
import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.backup import backup, fsync_directory, restore

NAME = re.compile(r"^archer-managed-(\d{8}T\d{6}Z)-([0-9a-f]{32})\.arbk$")


def rotate(data_dir: Path, files_root: Path, directory: Path, key_file: Path, *, now=None):
    """Publish a backup before pruning, restricted to a private dedicated directory."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Rotation time must be timezone-aware")
    now = now.astimezone(UTC)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("Backup directory must be owned, private, and not a symlink")
    if directory.resolve().is_relative_to(data_dir.resolve()) or directory.resolve().is_relative_to(
        files_root.resolve()
    ):
        raise ValueError("Backup directory must be outside source roots")
    if key_file.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Backup key must be outside the rotation directory")
    lock_path = directory / ".archer-rotation.lock"
    lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = f"archer-managed-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.arbk"
        receipt = backup(data_dir, files_root, directory / name, key_file)
        # Do not discard an older recovery point until the new archive has
        # passed authentication, manifest, database, and restore checks.
        with tempfile.TemporaryDirectory(prefix=".archer-restore-check-", dir=directory) as temporary:
            restore(directory / name, Path(temporary) / "restored", key_file)
        cutoff = now - timedelta(days=7)
        removed = 0
        for entry in directory.iterdir():
            match = NAME.fullmatch(entry.name)
            if not match or entry.name == name:
                continue
            try:
                timestamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                st = entry.lstat()
            except (ValueError, FileNotFoundError):
                continue
            if timestamp < cutoff and stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and st.st_nlink == 1:
                entry.unlink()
                removed += 1
        if removed:
            fsync_directory(directory)
        return {"archive": name, "removed": removed, "retention_days": 7, **receipt}
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--files-root", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rotate(args.data_dir, args.files_root, args.directory, args.key_file), sort_keys=True))


if __name__ == "__main__":
    main()
