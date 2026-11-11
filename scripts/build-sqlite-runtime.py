#!/usr/bin/env python3
"""Build a pinned private Linux SQLite shared library; never install host packages."""

import argparse
import hashlib
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

VERSION = "3.54.1"
URL = "https://www.sqlite.org/2026/sqlite-amalgamation-3540100.zip"
ARCHIVE_SHA3 = "92281c86e2957f9b9305890f9ac04204caa7b677cc868dc326d942689df28e30"
SOURCE_SHA3 = "e2b869d3003b33c942b7dd7063604fbc53553e1b66609570df39b9b7b558ee81"
SOURCE_ID = "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
FLAGS = [
    "-O2",
    "-fPIC",
    "-shared",
    "-Wl,-soname,libsqlite3.so.0",
    "-DSQLITE_THREADSAFE=1",
    "-DSQLITE_USE_URI=1",
    "-DSQLITE_ENABLE_FTS5",
    "-DSQLITE_ENABLE_RTREE",
    "-DSQLITE_ENABLE_COLUMN_METADATA",
    "-DSQLITE_ENABLE_DBSTAT_VTAB",
    "-DSQLITE_ENABLE_MATH_FUNCTIONS",
    "-DSQLITE_SECURE_DELETE",
]


def build(destination):
    if platform.system() != "Linux" or destination.exists() or destination.is_symlink():
        raise ValueError("Requires Linux and a new destination")
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(URL, timeout=30) as response:
        if response.geturl() != URL:
            raise ValueError("Unexpected source redirect")
        archive = response.read(4 * 1024 * 1024 + 1)
    if len(archive) > 4 * 1024 * 1024 or hashlib.sha3_256(archive).hexdigest() != ARCHIVE_SHA3:
        raise ValueError("Official SQLite source archive digest mismatch")
    with tempfile.TemporaryDirectory(prefix=".archer-sqlite-", dir=destination.parent) as temporary:
        work = Path(temporary)
        payload = work / "payload"
        (payload / "lib").mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            member = bundle.getinfo("sqlite-amalgamation-3540100/sqlite3.c")
            if member.file_size > 16 * 1024 * 1024:
                raise ValueError("SQLite source size limit")
            source = bundle.read(member)
        if hashlib.sha3_256(source).hexdigest() != SOURCE_SHA3:
            raise ValueError("Official SQLite C source digest mismatch")
        (work / "sqlite3.c").write_bytes(source)
        library = payload / "lib/libsqlite3.so.0"
        subprocess.run(
            ["cc", *FLAGS, str(work / "sqlite3.c"), "-o", str(library), "-lpthread", "-ldl", "-lm"],
            check=True,
            timeout=180,
        )
        environment = dict(os.environ, LD_LIBRARY_PATH=str(payload / "lib"))
        probe = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import sqlite3,json; c=sqlite3.connect(':memory:'); print(json.dumps({'version':sqlite3.sqlite_version,'source_id':c.execute('select sqlite_source_id()').fetchone()[0],'threadsafety':sqlite3.threadsafety,'compile_options':[r[0] for r in c.execute('pragma compile_options')]}))",
                ],
                env=environment,
                text=True,
                timeout=10,
            )
        )
        if probe["version"] != VERSION or probe["source_id"] != SOURCE_ID or probe["threadsafety"] != 3:
            raise ValueError("Python did not load the pinned threadsafe SQLite runtime")
        manifest = {
            "format": 1,
            "built_at": datetime.now(UTC).isoformat(),
            "architecture": platform.machine(),
            "source_url": URL,
            "archive_sha3_256": ARCHIVE_SHA3,
            "source_sha3_256": SOURCE_SHA3,
            "compiler": subprocess.check_output(["cc", "--version"], text=True).splitlines()[0],
            "flags": FLAGS,
            "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
            **probe,
        }
        (payload / "sqlite-runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        from app.backup import publish_directory

        publish_directory(payload, destination)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    print(json.dumps(build(parser.parse_args().destination)))
