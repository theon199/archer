"""Identity of the actual dynamically loaded Linux SQLite runtime."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path


def runtime_identity():
    libraries = set()
    for line in Path("/proc/self/maps").read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and fields[5].startswith("/") and Path(fields[5]).name.startswith("libsqlite3.so"):
            libraries.add(Path(fields[5]).resolve(strict=True))
    if len(libraries) != 1:
        raise ValueError("Expected one identifiable dynamically loaded Linux SQLite library")
    with closing(sqlite3.connect(":memory:")) as db:
        source_id = db.execute("SELECT sqlite_source_id()").fetchone()[0]
    return {
        "version": sqlite3.sqlite_version,
        "source_id": source_id,
        "library_sha256": hashlib.sha256(libraries.pop().read_bytes()).hexdigest(),
    }
