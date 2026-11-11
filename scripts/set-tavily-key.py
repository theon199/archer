#!/usr/bin/env python3
"""Save a Tavily key privately after prompting without terminal echo."""

import getpass
import os
import stat
from pathlib import Path


def main():
    directory = Path.home() / ".config" / "archer"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077 or not stat.S_ISDIR(info.st_mode):
            raise SystemExit("Archer config directory must be owned by you and private (mode 0700).")
        key = getpass.getpass("Tavily API key (input hidden): ")
        if not 1 <= len(key) <= 512 or any(not 33 <= ord(char) <= 126 for char in key):
            raise SystemExit("The key must be 1–512 printable ASCII characters with no spaces.")
        if key != getpass.getpass("Confirm Tavily API key (input hidden): "):
            raise SystemExit("Keys did not match; nothing was saved.")
        fd = os.open("tavily-api-key", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(key.encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink("tavily-api-key", dir_fd=descriptor)
            raise
        print("Tavily key saved privately. In Archer, refresh Tools and review the search permission.")
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
