"""Permission and note invariants: actual SQLite/files, no external integrations faked."""

import asyncio
import importlib
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app.plugins.files import ApprovedFilesPlugin
from app.plugins.registry import registry
from app.storage.database import get_db, init_db

ACCOUNT = "acc_sri_owner"


async def grant(
    name="personal_notes",
    actions=("create", "get", "list", "update", "delete", "history"),
    account=ACCOUNT,
    version=None,
):
    version = version or registry.version(name)
    async with get_db() as db:
        for action in actions:
            await db.execute(
                "INSERT OR REPLACE INTO tool_grants (account_id,tool_name,action,tool_version,enabled) VALUES (?,?,?,?,1)",
                (account, name, action, version),
            )
        await db.commit()


async def notes(args, op=None, account=ACCOUNT):
    return await registry.dispatch("personal_notes", account, args, operation_id=op)


@pytest.mark.asyncio
async def test_default_deny_invalid_arguments_unknown_tool_and_redaction():
    denied = await notes({"action": "create", "content": "secret-value"})
    assert denied["status"] == "denied"
    await grant()
    for args in [
        {"action": "create", "account_id": "other"},
        {"action": "delete", "id": "x", "expected_revision": True},
        {"action": "delete", "id": "x", "expected_revision": 1, "content": "hidden update"},
        {"action": "update", "id": "x"},
        {"action": "list", "title": "extra"},
    ]:
        assert (await notes(args))["status"] == "denied"
    assert (await registry.dispatch("mcp.unreviewed", ACCOUNT, {}))["status"] == "denied"
    async with get_db() as db:
        rows = await (await db.execute("SELECT arguments,permission_decision FROM journal_entries")).fetchall()
        assert all(row["permission_decision"] == "denied" for row in rows)
        assert "secret-value" not in str([dict(row) for row in rows])
        assert (await (await db.execute("SELECT COUNT(*) FROM notes")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_brave_grant_does_not_authorize_tavily_search():
    await grant("web_search", ("search",), version="1")
    result = await registry.dispatch("web_search", ACCOUNT, {"query": "test"})
    assert result["status"] == "denied"
    await grant("web_search", ("search",))
    result = await registry.dispatch("web_search", ACCOUNT, {"query": "test"})
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_atomic_note_idempotency_revision_conflict_soft_delete_and_history():
    await grant()
    args = {"action": "create", "title": "Plan", "content": "personal body"}
    first, duplicate = await asyncio.gather(notes(args, "create-1"), notes(args, "create-1"))
    assert first == duplicate and first["revision"] == 1
    note_id = first["note_id"]
    assert (await notes({**args, "content": "changed"}, "create-1"))["status"] == "conflict"
    changed = await notes(
        {"action": "update", "id": note_id, "expected_revision": 1, "content": "revised", "tags": ["life"]}, "update-1"
    )
    assert changed["revision"] == 2
    assert (await notes({"action": "delete", "id": note_id, "expected_revision": 1}))["status"] == "conflict"
    deleted = await notes({"action": "delete", "id": note_id, "expected_revision": 2}, "delete-1")
    assert deleted["revision"] == 3
    assert (await notes({"action": "delete", "id": note_id, "expected_revision": 2}, "delete-1")) == deleted
    assert (await notes({"action": "get", "id": note_id}))["status"] == "not_found"
    history = await notes({"action": "history", "id": note_id})
    assert [r["revision"] for r in history["revisions"]] == [1, 2, 3]
    assert history["revisions"][-1]["deleted_at"]
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM notes")).fetchone())[0] == 1
        row = await (
            await db.execute(
                "SELECT j.* FROM journal_entries j JOIN tool_operations o ON o.journal_id=j.id WHERE o.operation_id='create-1'"
            )
        ).fetchone()
        assert row["permission_decision"] == "allowed"
        assert json.loads(row["outcome"])["success"]
        assert "personal body" not in row["arguments"]


@pytest.mark.asyncio
async def test_journal_failure_rolls_back_note_and_operation():
    await grant()
    async with get_db() as db:
        await db.execute(
            "CREATE TRIGGER fail_journal BEFORE INSERT ON journal_entries BEGIN SELECT RAISE(ABORT, 'test journal failure'); END"
        )
        await db.commit()
    with pytest.raises(Exception, match="test journal failure"):
        await notes({"action": "create", "content": "must roll back"}, "rollback")
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM notes")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM tool_operations")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM note_revisions")).fetchone())[0] == 0
        await db.execute("DROP TRIGGER fail_journal")
        await db.commit()
    await init_db()  # Reopen storage after the simulated write failure.
    args = {"action": "create", "content": "must roll back"}
    result = await notes(args, "rollback")
    assert result["success"] and result["revision"] == 1
    assert await notes(args, "rollback") == result
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM notes")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM note_revisions")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM tool_operations")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM journal_entries")).fetchone())[0] == 1


@pytest.mark.asyncio
async def test_sqlite_full_note_write_rolls_back_and_can_be_retried(monkeypatch):
    await grant()
    registry_module = importlib.import_module("app.plugins.registry")
    original_get_db = registry_module.get_db

    @asynccontextmanager
    async def full_database():
        async with original_get_db() as db:
            pages = (await (await db.execute("PRAGMA page_count")).fetchone())[0]
            await db.execute(f"PRAGMA max_page_count={pages}")
            yield db

    args = {"action": "create", "content": "x" * 64000}
    with monkeypatch.context() as patch:
        patch.setattr(registry_module, "get_db", full_database)
        with pytest.raises(sqlite3.OperationalError) as failure:
            await notes(args, "full-retry")
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_FULL

    async with get_db() as db:
        for table in ("notes", "note_revisions", "tool_operations", "journal_entries"):
            assert (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] == 0
        assert (await (await db.execute("PRAGMA integrity_check")).fetchone())[0] == "ok"

    created = await notes(args, "full-retry")
    assert created["success"] and created["revision"] == 1
    assert await notes(args, "full-retry") == created
    async with get_db() as db:
        for table in ("notes", "note_revisions", "tool_operations", "journal_entries"):
            assert (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] == 1


@pytest.mark.asyncio
async def test_revoked_or_wrong_version_grants_and_foreign_notes():
    await grant(version="old")
    assert (await notes({"action": "list"}))["status"] == "denied"
    await grant()
    created = await notes({"action": "create"})
    async with get_db() as db:
        await db.execute("INSERT INTO accounts (id,username,password_hash) VALUES ('other','other','invalid')")
        await db.execute("UPDATE tool_grants SET enabled=0 WHERE action=?", ("list",))
        await db.commit()
    assert (await notes({"action": "list"}))["status"] == "denied"
    await grant(account="other")
    assert (await notes({"action": "get", "id": created["note_id"]}, account="other"))["status"] == "not_found"
    assert (
        await notes(
            {"action": "update", "id": created["note_id"], "expected_revision": 1, "title": "hijack"}, account="other"
        )
    )["status"] == "not_found"


@pytest.mark.asyncio
async def test_files_account_root_symlinks_sibling_prefix_and_special_file(tmp_path: Path):
    root = tmp_path / "files"
    own = root / ACCOUNT
    own.mkdir(parents=True)
    (root / "other").mkdir()
    (own / "ok.txt").write_text("permitted")
    (root / "other" / "private").write_text("private")
    (own / "escape").symlink_to(root / "other", target_is_directory=True)
    plugin = ApprovedFilesPlugin(root)
    assert (await plugin.execute(ACCOUNT, {"action": "read", "path": "ok.txt"}))["content"] == "permitted"
    for path in ["../other/private", "escape/private", "/etc/passwd", "../acc_sri_owner-extra/private"]:
        assert not (await plugin.execute(ACCOUNT, {"action": "read", "path": path}))["success"]
    assert not (await plugin.execute("other", {"action": "read", "path": "ok.txt"}))["success"]
    import os

    os.mkfifo(own / "fifo")
    assert not (await asyncio.wait_for(plugin.execute(ACCOUNT, {"action": "read", "path": "fifo"}), 1))["success"]


@pytest.mark.asyncio
async def test_search_unavailable_is_allowed_failure_not_permission_denial():
    await grant("web_search", ("search",))
    result = await registry.dispatch("web_search", ACCOUNT, {"query": "test query"})
    assert result["status"] == "unavailable" and result["permission_decision"] == "allowed"
    async with get_db() as db:
        row = await (
            await db.execute("SELECT permission_decision,outcome FROM journal_entries ORDER BY rowid DESC LIMIT 1")
        ).fetchone()
        assert row["permission_decision"] == "allowed"
        assert json.loads(row["outcome"])["status"] == "unavailable"


@pytest.mark.asyncio
async def test_owner_only_grants_and_note_routes_require_mutation_key():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
    ) as client:
        assert (await client.put("/api/permissions/personal_notes/create", json={"enabled": True})).status_code == 403
        async with get_db() as db:
            await db.execute("UPDATE accounts SET is_owner=1 WHERE id=?", (ACCOUNT,))
            await db.commit()
        assert (await client.put("/api/permissions/personal_notes/create", json={"enabled": True})).status_code == 200
        assert (await client.post("/api/notes", json={"title": "plan"})).status_code == 422
        response = await client.post("/api/notes", json={"title": "plan"}, headers={"Idempotency-Key": "api-create"})
        assert response.status_code == 200
        assert (
            await client.post("/api/notes", json={"title": "plan"}, headers={"Idempotency-Key": "api-create"})
        ).json() == response.json()
        assert (await client.put("/api/permissions/mcp.unknown/run", json={"enabled": True})).status_code == 404


@pytest.mark.asyncio
async def test_additive_migration_preserves_legacy_notes(tmp_path, monkeypatch):
    import aiosqlite

    monkeypatch.setattr(settings, "data_dir", tmp_path / "legacy")
    async with aiosqlite.connect(str(settings.database_path)) as db:
        await db.executescript(
            "CREATE TABLE accounts (id TEXT PRIMARY KEY,username TEXT,password_hash TEXT,is_owner INTEGER); INSERT INTO accounts VALUES ('legacy','legacy','invalid',0); CREATE TABLE notes (id TEXT PRIMARY KEY,account_id TEXT,title TEXT,content TEXT,tags TEXT,created_at TEXT,updated_at TEXT); INSERT INTO notes VALUES ('n','legacy','keep','body','[]',NULL,NULL);"
        )
    await init_db()
    await init_db()
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM notes WHERE id='n'")).fetchone()
        assert row["content"] == "body" and row["revision"] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM note_revisions")).fetchone())[0] == 1


@pytest.mark.asyncio
async def test_journal_read_transitions_replay_and_owner_isolation():
    from app.journal.service import journal

    await grant("web_search", ("search",))
    await registry.dispatch("web_search", ACCOUNT, {"query": "not logged"}, operation_id="read-1")
    events = await journal.events_after(ACCOUNT, 0)
    assert len(events) == 2
    assert json.loads(events[0]["data"])["outcome"]["status"] == "running"
    assert json.loads(events[1]["data"])["outcome"]["status"] == "unavailable"
    assert "not logged" not in str(events)
    assert len(await journal.events_after(ACCOUNT, events[0]["seq"])) == 1
    assert await journal.events_after("other-account", 0) == []


@pytest.mark.asyncio
async def test_worker_process_note_commit_and_journal_survive_response_loss():
    import os
    import subprocess
    import sys

    from app.journal.service import journal

    await grant()
    code = """
import asyncio, os
from app.plugins.registry import registry
async def write():
    result=await registry.dispatch("personal_notes","acc_sri_owner",{"action":"create","content":"process test"},operation_id="lost-response")
    assert result["success"]
    os._exit(24)
asyncio.run(write())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code],
        env=dict(os.environ, ARCHER_DATA_DIR=str(settings.data_dir)),
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 24, result.stderr.decode()
    replay = await notes({"action": "create", "content": "process test"}, "lost-response")
    assert replay["success"]
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM notes")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM tool_operations")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM journal_entries")).fetchone())[0] == 1
    assert len(await journal.events_after(ACCOUNT, 0)) == 1


@pytest.mark.asyncio
async def test_concurrent_note_updates_have_one_winner():
    await grant()
    created = await notes({"action": "create"})
    first, second = await asyncio.gather(
        notes({"action": "update", "id": created["note_id"], "expected_revision": 1, "content": "first"}, "race-1"),
        notes({"action": "update", "id": created["note_id"], "expected_revision": 1, "content": "second"}, "race-2"),
    )
    assert sorted([first["status"], second["status"]]) == ["conflict", "succeeded"]
    history = await notes({"action": "history", "id": created["note_id"]})
    assert [r["revision"] for r in history["revisions"]] == [1, 2]
