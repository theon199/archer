"""Real encrypted file/database recovery tests; no fake providers or owner data."""

import asyncio
import io
import json
import os
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from httpx import ASGITransport, AsyncClient

from app.auth import provision_owner
from app.backup import backup, keygen, load_key, publish_directory, restore, unpack
from app.backup_rotation import rotate
from app.config import settings
from app.main import app
from app.memory.extractor import extractor
from app.plugins.registry import record, registry
from app.runs.service import append_event, claim, submit_run
from app.runs.worker import Worker
from app.storage.database import get_db

PASSWORD = "synthetic-backup-owner-password"


@pytest.mark.asyncio
async def test_local_rotation_retains_seven_days_and_restores(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    directory = tmp_path / "managed"
    directory.mkdir(mode=0o700)
    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    old = rotate(
        fixture["source"], settings.approved_files_root, directory, fixture["key"], now=now - timedelta(days=8)
    )
    boundary = rotate(
        fixture["source"], settings.approved_files_root, directory, fixture["key"], now=now - timedelta(days=7)
    )
    other = directory / "unrelated.arbk"
    other.write_text("not managed")
    link = directory / "archer-managed-20200101T000000Z-00000000000000000000000000000000.arbk"
    link.symlink_to(other)
    result = rotate(fixture["source"], settings.approved_files_root, directory, fixture["key"], now=now)
    assert result["removed"] == 1
    assert not (directory / old["archive"]).exists()
    assert (directory / boundary["archive"]).is_file()
    assert other.read_text() == "not managed" and link.is_symlink()
    restored = tmp_path / "rotation-restore"
    restore(directory / result["archive"], restored, fixture["key"])
    assert (restored / "archer.db").is_file()


@pytest.mark.asyncio
async def test_local_rotation_never_prunes_if_new_backup_fails(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    directory = tmp_path / "managed"
    directory.mkdir(mode=0o700)
    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    old = rotate(
        fixture["source"], settings.approved_files_root, directory, fixture["key"], now=now - timedelta(days=8)
    )
    with pytest.raises(FileNotFoundError):
        rotate(fixture["source"], settings.approved_files_root, directory, tmp_path / "missing.key", now=now)
    assert (directory / old["archive"]).is_file()


@pytest.mark.asyncio
async def test_local_rotation_never_prunes_if_restore_check_fails(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    directory = tmp_path / "managed"
    directory.mkdir(mode=0o700)
    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    old = rotate(
        fixture["source"], settings.approved_files_root, directory, fixture["key"], now=now - timedelta(days=8)
    )

    def fail_restore(*args, **kwargs):
        raise ValueError("synthetic restore verification failure")

    monkeypatch.setattr("app.backup_rotation.restore", fail_restore)
    with pytest.raises(ValueError, match="synthetic restore verification"):
        rotate(fixture["source"], settings.approved_files_root, directory, fixture["key"], now=now)
    assert (directory / old["archive"]).is_file()


@pytest.mark.asyncio
async def test_local_rotation_rejects_unsafe_directory_and_key(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    directory = tmp_path / "managed"
    directory.mkdir(mode=0o700)
    link = tmp_path / "linked-directory"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="private"):
        rotate(fixture["source"], settings.approved_files_root, link, fixture["key"])
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        rotate(fixture["source"], settings.approved_files_root, directory, fixture["key"])
    directory.chmod(0o700)
    with pytest.raises(ValueError, match="outside the rotation"):
        rotate(fixture["source"], settings.approved_files_root, directory, directory / "key")


async def seed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    monkeypatch.setattr(settings, "data_dir", source)
    monkeypatch.setattr(settings, "approved_files_root", source / "approved_files")
    await provision_owner("drill-owner", PASSWORD)
    async with get_db() as db:
        account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
        for action in ["create", "get", "list", "update"]:
            await db.execute(
                "INSERT INTO tool_grants (account_id,tool_name,action,tool_version,enabled) VALUES (?,?,?,?,1)",
                (account, "personal_notes", action, "1"),
            )
        await db.execute(
            "INSERT INTO conversations (id,account_id,title,provider) VALUES (?,?,?,?)",
            ("evidence-conv", account, "Synthetic preference", "local-ollama"),
        )
        await db.execute(
            "INSERT INTO messages (id,conversation_id,account_id,role,content) VALUES ('evidence','evidence-conv',?,'user','I prefer concise answers.')",
            (account,),
        )
        await extractor.enqueue_in_transaction(db, account, "evidence")
        await db.commit()
    assert await extractor.process_one()
    memory = (await extractor.get_memories(account))[0]
    await extractor.delete_memory(account, memory["id"], 1)
    note = await registry.dispatch(
        "personal_notes",
        account,
        {"action": "create", "title": "Synthetic restore note", "content": "backup body"},
        operation_id="saved-note",
    )
    owner_dir = settings.approved_files_root / account
    owner_dir.mkdir(parents=True)
    (owner_dir / "approved.txt").write_text("Synthetic approved file")
    key = tmp_path / "backup.key"
    keygen(key)
    output = tmp_path / "snapshot.arbk"
    queued = await submit_run(account, "Synthetic queued request", None, "local-ollama", "queued")
    with Worker() as worker:
        running = await claim(worker.owner)
        await append_event(running, "tool_call", {"tool": "personal_notes", "arguments": {"action": "create"}})
    await submit_run(account, "Another synthetic queued request", None, "local-ollama", "also-queued")
    async with get_db() as db:
        entry = await record(
            db,
            account,
            "personal_notes",
            {"action": "create"},
            "allowed",
            {"success": False, "status": "running"},
            running["id"],
            running["conversation_id"],
        )
        await db.execute(
            "INSERT INTO tool_operations (account_id,operation_id,tool_name,tool_version,arguments_digest,status,run_id,journal_id) VALUES (?,?,'personal_notes','1','synthetic','running',?,?)",
            (account, "unfinished-note", running["id"], entry),
        )
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/auth/login", json={"username": "drill-owner", "password": PASSWORD})
        assert response.status_code == 200
        token = response.json()["access_token"]
    return {
        "source": source,
        "account": account,
        "note": note,
        "key": key,
        "output": output,
        "run": queued,
        "token": token,
    }


@pytest.mark.asyncio
async def test_online_wal_snapshot_clean_restore_and_recovery_controls(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    # Keep an observer open, so committed WAL pages are present during backup.
    observer = sqlite3.connect(settings.database_path)
    observer.execute("PRAGMA journal_mode=WAL")
    try:
        updated = await registry.dispatch(
            "personal_notes",
            fixture["account"],
            {
                "action": "update",
                "id": fixture["note"]["note_id"],
                "expected_revision": 1,
                "content": "committed WAL body",
            },
            operation_id="wal-update",
        )
        assert updated["revision"] == 2
        assert Path(str(settings.database_path) + "-wal").exists()
        result = await asyncio.to_thread(
            backup, fixture["source"], settings.approved_files_root, fixture["output"], fixture["key"]
        )
    finally:
        observer.close()
    assert result["files"] == 2 and result["encrypted_bytes"] > 0
    assert b"committed WAL body" not in fixture["output"].read_bytes()
    assert fixture["output"].stat().st_mode & 0o777 == 0o600
    destination = tmp_path / "restored"
    outcome = await asyncio.to_thread(restore, fixture["output"], destination, fixture["key"])
    assert outcome["sessions_revoked"] and outcome["unfinished_runs_quarantined"]
    assert (
        destination / "approved_files" / fixture["account"] / "approved.txt"
    ).read_text() == "Synthetic approved file"
    with sqlite3.connect(destination / "archer.db") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
        assert db.execute("SELECT content,revision FROM notes").fetchone() == ("committed WAL body", 2)
        assert db.execute("SELECT COUNT(*) FROM note_revisions").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tool_grants").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM tool_grants WHERE enabled=1").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert db.execute("SELECT subject_key FROM memory_suppressions").fetchone()[0] == "response_length"
        assert db.execute("SELECT learning_enabled,retrieval_enabled FROM memory_settings").fetchone() == (0, 0)
        assert {r[0] for r in db.execute("SELECT status FROM runs")} == {"needs_reconciliation"}
        assert (
            db.execute("SELECT status FROM tool_operations WHERE operation_id='unfinished-note'").fetchone()[0]
            == "needs_reconciliation"
        )
        assert db.execute("SELECT COUNT(*) FROM run_events WHERE event='done'").fetchone()[0] == 2
    monkeypatch.setattr(settings, "data_dir", destination)
    monkeypatch.setattr(settings, "approved_files_root", destination / "approved_files")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.get("/api/notes", headers={"Authorization": f"Bearer {fixture['token']}"})
        ).status_code == 401
        response = await client.post("/api/auth/login", json={"username": "drill-owner", "password": PASSWORD})
        assert response.status_code == 200
        renewed = await client.put(
            "/api/permissions/personal_notes/list",
            headers={"Authorization": f"Bearer {response.json()['access_token']}"},
            json={"enabled": True},
        )
        assert renewed.status_code == 200
        notes = await client.get("/api/notes", headers={"Authorization": f"Bearer {response.json()['access_token']}"})
        assert notes.status_code == 200 and notes.json()[0]["revision"] == 2
    with Worker() as worker:
        assert not await worker.run_one()  # No restored job can execute itself.
    with pytest.raises(FileExistsError):
        restore(fixture["output"], destination, fixture["key"])


@pytest.mark.asyncio
async def test_wrong_key_tampering_truncation_never_publish(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    backup(fixture["source"], settings.approved_files_root, fixture["output"], fixture["key"])
    wrong = tmp_path / "wrong.key"
    keygen(wrong)
    destination = tmp_path / "failed-restore"
    with pytest.raises(InvalidTag):
        restore(fixture["output"], destination, wrong)
    assert not destination.exists()
    raw = bytearray(fixture["output"].read_bytes())
    raw[-1] ^= 1
    damaged = tmp_path / "damaged.arbk"
    damaged.write_bytes(raw)
    with pytest.raises(InvalidTag):
        restore(damaged, destination, fixture["key"])
    assert not destination.exists()
    damaged.write_bytes(raw[:10])
    with pytest.raises(ValueError):
        restore(damaged, destination, fixture["key"])
    assert not destination.exists()


@pytest.mark.asyncio
async def test_source_symlinks_output_overwrite_and_unsafe_key_refused(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    os.chmod(fixture["key"], 0o644)
    with pytest.raises(ValueError, match="mode"):
        load_key(fixture["key"])
    os.chmod(fixture["key"], 0o600)
    link = tmp_path / "key-link"
    link.symlink_to(fixture["key"])
    with pytest.raises(OSError):
        load_key(link)
    (settings.approved_files_root / "escape").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        backup(fixture["source"], settings.approved_files_root, fixture["output"], fixture["key"])
    assert not fixture["output"].exists()
    (settings.approved_files_root / "escape").unlink()
    backup(fixture["source"], settings.approved_files_root, fixture["output"], fixture["key"])
    before = fixture["output"].read_bytes()
    with pytest.raises(FileExistsError):
        backup(fixture["source"], settings.approved_files_root, fixture["output"], fixture["key"])
    assert fixture["output"].read_bytes() == before
    with pytest.raises(ValueError, match="outside"):
        backup(fixture["source"], settings.approved_files_root, fixture["source"] / "bad.arbk", fixture["key"])


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/tmp/escape", tarfile.REGTYPE),
        ("approved_files/link", tarfile.SYMTYPE),
        ("unknown", tarfile.REGTYPE),
    ],
)
def test_unsafe_archive_members_rejected(tmp_path, name, kind):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
        member.size = 0
        tar.addfile(member, io.BytesIO())
    destination = tmp_path / "stage"
    destination.mkdir()
    with pytest.raises(ValueError):
        unpack(raw.getvalue(), destination)
    assert not (tmp_path / "escape").exists()


def test_atomic_directory_publication_does_not_replace_existing_data(tmp_path):
    source = tmp_path / "stage"
    source.mkdir()
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("original")
    with pytest.raises(OSError):
        publish_directory(source, existing)
    assert (existing / "keep").read_text() == "original"
    assert source.exists()


@pytest.mark.asyncio
async def test_snapshot_hash_manifest_detects_wrong_content(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from app.backup import HEADER

    backup(fixture["source"], settings.approved_files_root, fixture["output"], fixture["key"])
    raw = fixture["output"].read_bytes()
    nonce = raw[len(HEADER) : len(HEADER) + 12]
    archive = AESGCM(load_key(fixture["key"])).decrypt(nonce, raw[len(HEADER) + 12 :], HEADER)
    rewritten = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as incoming,
        tarfile.open(fileobj=rewritten, mode="w") as outgoing,
    ):
        for member in incoming:
            data = incoming.extractfile(member).read()
            if member.name == "manifest.json":
                manifest = json.loads(data)
                manifest["files"]["archer.db"]["sha256"] = "0" * 64
                data = json.dumps(manifest).encode()
                member.size = len(data)
            outgoing.addfile(member, io.BytesIO(data))
    stage = tmp_path / "hash-stage"
    stage.mkdir()
    with pytest.raises(ValueError, match="manifest/hash"):
        unpack(rewritten.getvalue(), stage)


@pytest.mark.asyncio
async def test_manifest_limit_rejects_unrestorable_backup_before_publication(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    monkeypatch.setattr("app.backup.MAX_MANIFEST", 32)
    with pytest.raises(ValueError, match="manifest exceeds"):
        backup(fixture["source"], settings.approved_files_root, fixture["output"], fixture["key"])
    assert not fixture["output"].exists()


@pytest.mark.asyncio
async def test_duplicate_archive_member_rejected(tmp_path, monkeypatch):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for _ in range(2):
            member = tarfile.TarInfo("approved_files/account/file")
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
    destination = tmp_path / "stage"
    destination.mkdir()
    with pytest.raises(ValueError, match="duplicate"):
        unpack(raw.getvalue(), destination)


@pytest.mark.asyncio
async def test_restore_snapshot_from_before_worker_status_table(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    with sqlite3.connect(settings.database_path) as db:
        db.execute("DROP TABLE worker_status")
        db.execute("DROP TABLE mcp_resources")
    backup(settings.data_dir, settings.approved_files_root, fixture["output"], fixture["key"])
    target = tmp_path / "legacy-restored"
    restore(fixture["output"], target, fixture["key"])
    with sqlite3.connect(target / "archer.db") as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_restored_resource_records_cannot_clean_source_containers(tmp_path, monkeypatch):
    fixture = await seed(tmp_path, monkeypatch)
    async with get_db() as db:
        await db.execute(
            "INSERT INTO mcp_resources (name,identity_token,account_id,operation_id,image_reference,worker_owner) VALUES ('archer-mcp-00000000000000000000000000000000','token',?,'unfinished-note','sha256:test-only','test-owner')",
            (fixture["account"],),
        )
        await db.commit()
    backup(settings.data_dir, settings.approved_files_root, fixture["output"], fixture["key"])
    target = tmp_path / "restored-resource-db"
    restore(fixture["output"], target, fixture["key"])
    monkeypatch.setattr(settings, "data_dir", target)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Restored records must not touch the source Docker daemon")

    monkeypatch.setattr("app.plugins.mcp_connector.remove_owned_container", forbidden)
    with Worker() as worker:
        assert await worker.recover_resources() == 0
    async with get_db() as db:
        assert (await (await db.execute("SELECT status FROM mcp_resources")).fetchone())[0] == "restored_unverified"
