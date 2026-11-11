"""Encrypted, bounded local backups and non-overwriting recovery.

No services, keys, files or snapshots are sent anywhere by this module.
"""

import argparse
import ctypes
import hashlib
import io
import json
import os
import sqlite3
import stat
import tarfile
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HEADER = b"ARCHER-BACKUP-1\n"
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_CONTENT = 256 * 1024 * 1024
MAX_FILES = 10000
MAX_MANIFEST = 1024 * 1024
CHUNK = 1024 * 1024


def keygen(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(AESGCM.generate_key(bit_length=256))
        stream.flush()
        os.fsync(stream.fileno())


def load_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Key must be an owned regular file with mode 0600 or stricter")
        key = stream.read(33)
    if len(key) != 32:
        raise ValueError("Backup key must contain exactly 32 bytes")
    return key


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(CHUNK):
            value.update(chunk)
    return value.hexdigest()


def check_db(path):
    with closing(sqlite3.connect(path)) as db:
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise ValueError("Backup database failed integrity checks")
        migrations = [r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        known = {"003_tool_permissions_note_revisions", "004_memory_evidence_epochs"}
        if set(migrations) != known:
            raise ValueError("Database schema is not supported by this backup version")
        revisions = [list(r) for r in db.execute("SELECT id,revision FROM notes ORDER BY id")]
    return {
        "migrations": migrations,
        "note_revision_digest": hashlib.sha256(json.dumps(revisions).encode()).hexdigest(),
    }


def copy_approved(root, destination):
    records = {}
    if not os.path.lexists(root):
        return records

    def walk(fd, relative):
        directory_before = os.fstat(fd)
        for name in sorted(os.listdir(fd)):
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            rel = relative / name
            if stat.S_ISLNK(before.st_mode):
                raise ValueError("Approved files contain a symlink; backup refused")
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    walk(child, rel)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CONTENT:
                raise ValueError("Unsupported or oversized approved file")
            target = destination / "approved_files" / rel
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(source, "rb") as incoming, open(target, "xb") as outgoing:
                os.chmod(target, 0o600)
                opened = os.fstat(incoming.fileno())
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("Approved file changed during backup")
                copied = 0
                while chunk := incoming.read(CHUNK):
                    copied += len(chunk)
                    if copied > MAX_CONTENT:
                        raise ValueError("Backup size limit exceeded")
                    outgoing.write(chunk)
                after = os.fstat(incoming.fileno())
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("Approved file changed during backup")
            records[target.relative_to(destination).as_posix()] = {"size": copied, "sha256": digest(target)}
            if len(records) > MAX_FILES or sum(r["size"] for r in records.values()) > MAX_CONTENT:
                raise ValueError("Backup size or file-count limit exceeded")

        directory_after = os.fstat(fd)
        if (directory_before.st_mtime_ns, directory_before.st_ctime_ns) != (
            directory_after.st_mtime_ns,
            directory_after.st_ctime_ns,
        ):
            raise ValueError("Approved directory changed during backup")

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(root_fd, Path())
    finally:
        os.close(root_fd)
    return records


def backup(data_dir: Path, files_root: Path, output: Path, key_file: Path):
    data_dir = data_dir.resolve()
    if (
        output.resolve().is_relative_to(data_dir)
        or key_file.resolve().is_relative_to(data_dir)
        or key_file.resolve().is_relative_to(files_root.resolve())
        or output.resolve().is_relative_to(files_root.resolve())
    ):
        raise ValueError("Key and backup output must be outside the source data/files roots")
    key = load_key(key_file)
    source = data_dir / "archer.db"
    if not source.is_file() or source.is_symlink():
        raise ValueError("Source database must exist and must not be a symlink")
    if os.path.lexists(output):
        raise FileExistsError("Backup output already exists")
    with tempfile.TemporaryDirectory(prefix=".archer-backup-", dir=output.parent) as work:
        stage = Path(work)
        payload = stage / "payload"
        payload.mkdir(mode=0o700)
        snapshot = payload / "archer.db"
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as incoming,
            closing(sqlite3.connect(snapshot)) as outgoing,
        ):
            incoming.backup(outgoing, pages=256, sleep=0.01)
        os.chmod(snapshot, 0o600)
        metadata = check_db(snapshot)
        files = copy_approved(files_root, payload)
        files["archer.db"] = {"size": snapshot.stat().st_size, "sha256": digest(snapshot)}
        if len(files) > MAX_FILES or sum(f["size"] for f in files.values()) > MAX_CONTENT:
            raise ValueError("Backup payload exceeds 256 MiB")
        manifest = {"format": 1, "created_at": datetime.now(UTC).isoformat(), "database": metadata, "files": files}
        archive = stage / "payload.tar"
        with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
            for name in sorted(files):
                tar.add(payload / name, arcname=name, recursive=False)
            data = json.dumps(manifest, sort_keys=True).encode()
            if len(data) > MAX_MANIFEST:
                raise ValueError("Backup manifest exceeds size limit")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
        if archive.stat().st_size > MAX_ARCHIVE:
            raise ValueError("Backup archive exceeds size limit")
        nonce = os.urandom(12)
        encrypted = HEADER + nonce + AESGCM(key).encrypt(nonce, archive.read_bytes(), HEADER)
        partial = stage / "encrypted.arbk"
        with open(partial, "xb") as stream:
            os.chmod(partial, 0o600)
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(partial, output)  # Atomic no-overwrite publication, even under a race.
        fsync_directory(output.parent)
    return {"format": 1, "files": len(files), "encrypted_bytes": output.stat().st_size, "sha256": digest(output)}


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def unpack(archive: bytes, destination: Path):
    if len(archive) > MAX_ARCHIVE:
        raise ValueError("Archive exceeds size limit")
    actual = {}
    manifest = None
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for index, member in enumerate(tar):
            path = PurePosixPath(member.name)
            name = path.as_posix()
            if (
                index > MAX_FILES
                or not member.isfile()
                or path.is_absolute()
                or ".." in path.parts
                or name != member.name
                or name in actual
            ):
                raise ValueError("Unsafe or duplicate backup member")
            if name != "manifest.json" and name != "archer.db" and not name.startswith("approved_files/"):
                raise ValueError("Unexpected backup member")
            total += member.size
            if member.size < 0 or total > MAX_CONTENT + MAX_MANIFEST:
                raise ValueError("Backup extraction limit exceeded")
            stream = tar.extractfile(member)
            if name == "manifest.json":
                if manifest is not None or member.size > MAX_MANIFEST:
                    raise ValueError("Invalid manifest")
                manifest = json.loads(stream.read())
                continue
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(target, "xb") as outgoing:
                os.chmod(target, 0o600)
                while chunk := stream.read(CHUNK):
                    outgoing.write(chunk)
            actual[name] = {"size": target.stat().st_size, "sha256": digest(target)}
    if not manifest or manifest.get("format") != 1 or manifest.get("files") != actual or "archer.db" not in actual:
        raise ValueError("Backup manifest/hash mismatch")
    if check_db(destination / "archer.db") != manifest["database"]:
        raise ValueError("Database revision manifest mismatch")
    return manifest


def quarantine(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM sessions")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_status'").fetchone():
            db.execute("DELETE FROM worker_status")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_resources'").fetchone():
            db.execute("UPDATE mcp_resources SET status='restored_unverified'")
        db.execute("DELETE FROM login_attempts")
        db.execute("UPDATE tool_grants SET enabled=0,revision=revision+1")
        db.execute("INSERT OR IGNORE INTO memory_settings (account_id) SELECT id FROM accounts")
        db.execute("UPDATE memory_settings SET learning_enabled=0,retrieval_enabled=0,epoch=epoch+1")
        db.execute("UPDATE memory_jobs SET status='invalidated' WHERE status='queued'")
        for run_id, account_id in db.execute(
            "SELECT id,account_id FROM runs WHERE status IN ('queued','generating','cancel_requested')"
        ).fetchall():
            db.execute(
                "UPDATE runs SET status='needs_reconciliation',error='Restored backup; review before resuming',generation=generation+1,lease_owner=NULL,lease_until=NULL WHERE id=?",
                (run_id,),
            )
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM run_events WHERE run_id=?", (run_id,)).fetchone()[0]
            for offset, event in enumerate(["state", "done"]):
                db.execute(
                    "INSERT INTO run_events (run_id,account_id,seq,event,data) VALUES (?,?,?,?,?)",
                    (run_id, account_id, seq + offset, event, json.dumps({"status": "needs_reconciliation"})),
                )
        for account_id, operation_id, journal_id in db.execute(
            "SELECT account_id,operation_id,journal_id FROM tool_operations WHERE status='running'"
        ).fetchall():
            result = json.dumps(
                {
                    "success": False,
                    "status": "needs_reconciliation",
                    "error": "Restored backup; operation outcome requires review",
                }
            )
            db.execute(
                "UPDATE tool_operations SET status='needs_reconciliation',result=? WHERE account_id=? AND operation_id=?",
                (result, account_id, operation_id),
            )
            db.execute(
                "UPDATE journal_entries SET outcome=?,error=? WHERE id=?",
                (result, "Restored backup; operation outcome requires review", journal_id),
            )
            row = db.execute("SELECT * FROM journal_entries WHERE id=?", (journal_id,)).fetchone()
            names = [c[1] for c in db.execute("PRAGMA table_info(journal_entries)")]
            data = dict(zip(names, row, strict=True))
            data["arguments"] = json.loads(data["arguments"])
            data["outcome"] = json.loads(result)
            db.execute(
                "INSERT INTO journal_events (account_id,journal_id,data) VALUES (?,?,?)",
                (account_id, journal_id, json.dumps(data)),
            )
        db.commit()
    check_db(path)


def publish_directory(source, destination):
    # Linux renameat2(RENAME_NOREPLACE): never replace even an empty existing dir.
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        raise OSError(ctypes.get_errno(), "Restore destination cannot be published without overwrite")
    fsync_directory(destination.parent)


def restore(artifact: Path, destination: Path, key_file: Path):
    if os.path.lexists(destination):
        raise FileExistsError("Restore destination already exists")
    if artifact.stat().st_size > MAX_ARCHIVE + len(HEADER) + 28:
        raise ValueError("Encrypted backup exceeds size limit")
    with open(artifact, "rb") as stream:
        encrypted = stream.read(MAX_ARCHIVE + len(HEADER) + 29)
    if len(encrypted) > MAX_ARCHIVE + len(HEADER) + 28:
        raise ValueError("Encrypted backup exceeds size limit")
    if not encrypted.startswith(HEADER) or len(encrypted) < len(HEADER) + 28:
        raise ValueError("Unsupported encrypted backup format")
    nonce = encrypted[len(HEADER) : len(HEADER) + 12]
    # Authentication completes before any archive member is inspected/extracted.
    plain = AESGCM(load_key(key_file)).decrypt(nonce, encrypted[len(HEADER) + 12 :], HEADER)
    with tempfile.TemporaryDirectory(prefix=".archer-restore-", dir=destination.parent) as work:
        stage = Path(work) / "payload"
        stage.mkdir(mode=0o700)
        manifest = unpack(plain, stage)
        quarantine(stage / "archer.db")
        (stage / "approved_files").mkdir(exist_ok=True, mode=0o700)
        (stage / "RESTORE-REVIEW.json").write_text(
            json.dumps(
                {
                    "format": 1,
                    "backup_created_at": manifest["created_at"],
                    "sessions_revoked": True,
                    "unfinished_runs_quarantined": True,
                    "learning_and_retrieval_disabled": True,
                    "grants_disabled": True,
                },
                indent=2,
            )
            + "\n"
        )
        os.chmod(stage / "RESTORE-REVIEW.json", 0o600)
        for file in stage.rglob("*"):
            if file.is_file():
                with open(file, "rb") as stream:
                    os.fsync(stream.fileno())
        for directory in sorted((p for p in stage.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            fsync_directory(directory)
        fsync_directory(stage)
        publish_directory(stage, destination)
    return {
        "restored": True,
        "sessions_revoked": True,
        "unfinished_runs_quarantined": True,
        "learning_and_retrieval_disabled": True,
        "grants_disabled": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("keygen")
    key.add_argument("--key-file", type=Path, required=True)
    save = commands.add_parser("create")
    save.add_argument("--data-dir", type=Path, required=True)
    save.add_argument("--files-root", type=Path, required=True)
    save.add_argument("--output", type=Path, required=True)
    save.add_argument("--key-file", type=Path, required=True)
    recover = commands.add_parser("restore")
    recover.add_argument("--input", type=Path, required=True)
    recover.add_argument("--destination", type=Path, required=True)
    recover.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "keygen":
            keygen(args.key_file)
            result = {"key_created": True}
        elif args.command == "create":
            result = backup(args.data_dir, args.files_root, args.output, args.key_file)
        else:
            result = restore(args.input, args.destination, args.key_file)
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        # Do not print personal paths, secret values, SQL or archive content.
        raise SystemExit(f"Backup operation failed ({type(exc).__name__}); no existing data was overwritten") from None


if __name__ == "__main__":
    main()
