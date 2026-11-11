"""Synthetic owner data; actual Argon2, SQLite transactions and HTTP auth."""

import asyncio
import json
import threading

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import hasher, provision_owner
from app.main import app
from app.owner_recovery import recover_owner
from app.runs.service import submit_run
from app.runs.worker import Worker
from app.storage.database import get_db

OLD = "old-synthetic-owner-password"
NEW = "new-synthetic-owner-password"


async def owner():
    await provision_owner("recovery-owner", OLD)
    async with get_db() as db:
        return (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]


@pytest.mark.asyncio
async def test_recovery_preserves_data_revokes_authority_and_journals():
    account = await owner()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/auth/login", json={"username": "recovery-owner", "password": OLD})
        token = login.json()["access_token"]
        async with get_db() as db:
            await db.execute(
                "INSERT INTO notes(id,account_id,title,content) VALUES ('recovery-note',?,'Synthetic','preserve')",
                (account,),
            )
            await db.execute(
                "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'personal_notes','list','1',1)",
                (account,),
            )
            await db.commit()
        result = await recover_owner(account, NEW)
        assert result["sessions_revoked"] == 1 and result["grants_disabled"] == 1
        assert (
            await client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 401
        assert (
            await client.post("/api/auth/login", json={"username": "recovery-owner", "password": OLD})
        ).status_code == 401
        assert (
            await client.post("/api/auth/login", json={"username": "recovery-owner", "password": NEW})
        ).status_code == 200
        async with get_db() as db:
            assert tuple(
                await (await db.execute("SELECT account_id,content FROM notes WHERE id='recovery-note'")).fetchone()
            ) == (account, "preserve")
            settings = await (
                await db.execute("SELECT * FROM memory_settings WHERE account_id=?", (account,))
            ).fetchone()
            assert settings["learning_enabled"] == settings["retrieval_enabled"] == 0
            events = await (
                await db.execute("SELECT data FROM journal_events WHERE account_id=?", (account,))
            ).fetchall()
            assert len(events) == 1 and json.loads(events[0][0])["tool_name"] == "owner_recovery"
            assert OLD not in events[0][0] and NEW not in events[0][0] and "argon2" not in events[0][0]
            assert (
                await (await db.execute("SELECT COUNT(*) FROM sessions WHERE account_id='acc_sri_owner'")).fetchone()
            )[0] == 1


@pytest.mark.asyncio
async def test_recovery_refuses_wrong_owner_live_worker_and_unsettled_run():
    account = await owner()
    with pytest.raises(ValueError, match="specified existing owner"):
        await recover_owner("acc_sri_owner", NEW)
    with Worker():
        with pytest.raises(RuntimeError, match="worker holds"):
            await recover_owner(account, NEW)
    await submit_run(account, "Synthetic queued task", None, "local-ollama", "recovery-pending")
    with pytest.raises(ValueError, match="Unsettled runs"):
        await recover_owner(account, NEW)
    async with get_db() as db:
        row = await (await db.execute("SELECT password_hash FROM accounts WHERE id=?", (account,))).fetchone()
        assert hasher.verify(row[0], OLD)
        assert (await (await db.execute("SELECT COUNT(*) FROM journal_entries")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_recovery_journal_failure_rolls_back(monkeypatch):
    account = await owner()

    async def fail(*args):
        raise RuntimeError("Synthetic journal commit-path failure")

    monkeypatch.setattr("app.owner_recovery.emit_journal_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic"):
        await recover_owner(account, NEW)
    async with get_db() as db:
        row = await (await db.execute("SELECT password_hash FROM accounts WHERE id=?", (account,))).fetchone()
        assert hasher.verify(row[0], OLD)
        assert (await (await db.execute("SELECT COUNT(*) FROM journal_entries")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_old_password_verification_cannot_mint_session_after_recovery(monkeypatch):
    account = await owner()
    verified, release = threading.Event(), threading.Event()
    original = hasher.verify

    def paused(self, *args):
        value = original(*args)
        verified.set()
        if not release.wait(10):
            raise RuntimeError("Synthetic race synchronization timed out")
        return value

    monkeypatch.setattr(type(hasher), "verify", paused)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        request = asyncio.create_task(
            client.post("/api/auth/login", json={"username": "recovery-owner", "password": OLD})
        )
        try:
            assert await asyncio.to_thread(verified.wait, 10)
            await recover_owner(account, NEW)
        finally:
            release.set()
        assert (await request).status_code == 401
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM sessions WHERE account_id=?", (account,))).fetchone())[
            0
        ] == 0
