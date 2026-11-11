"""Real SQL/crypto transfer and rollback on private synthetic data; no provider."""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.backup import keygen, restore
from app.config import settings
from app.legacy_migration import POLICY_VERSION, apply_plan, inspect_plan
from app.memory.extractor import extractor
from app.runs.worker import Worker
from app.storage.database import get_db
from tests.test_legacy_migration import dump, seed


async def rich_seed(owner):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO conversations(id,account_id,title,provider) VALUES ('legacy-conv','acc_sri_owner','Synthetic','local-ollama')"
        )
        await db.execute(
            "INSERT INTO messages(id,conversation_id,account_id,role,content) VALUES ('legacy-message','legacy-conv','acc_sri_owner','user','Synthetic old evidence')"
        )
        await db.execute(
            "INSERT INTO runs(id,account_id,conversation_id,client_operation_id,request_digest,message,provider,status) VALUES ('legacy-run','acc_sri_owner','legacy-conv','legacy-client-op','synthetic','Synthetic','local-ollama','failed')"
        )
        await db.execute(
            "INSERT INTO run_events(run_id,account_id,seq,event,data) VALUES ('legacy-run','acc_sri_owner',1,'done','{\"status\":\"failed\"}')"
        )
        await db.execute(
            "INSERT OR IGNORE INTO note_revisions(note_id,account_id,revision,title,content,tags) SELECT id,account_id,revision,title,content,tags FROM notes"
        )
        await db.execute(
            "INSERT INTO notes(id,account_id,title,content) VALUES ('owner-note',?,'Owner note','Owner content')",
            (owner,),
        )
        await db.execute(
            "INSERT INTO memories(id,account_id,memory_type,content,evidence,subject_key,value) VALUES ('legacy-memory','acc_sri_owner','preference','Synthetic old preference','legacy-message','response_length','concise')"
        )
        await db.execute(
            "INSERT INTO memory_evidence(id,memory_id,account_id,source_message_id,source_kind,value,quote) VALUES ('legacy-evidence','legacy-memory','acc_sri_owner','legacy-message','user_message','concise','Synthetic old evidence')"
        )
        await db.execute(
            "INSERT INTO memory_jobs(account_id,message_id,epoch) VALUES ('acc_sri_owner','legacy-message',0)"
        )
        for account in ["acc_sri_owner", owner]:
            await db.execute(
                "INSERT INTO memory_suppressions(account_id,subject_key,source_digest) VALUES (?,'units','synthetic')",
                (account,),
            )
            await db.execute(
                "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'personal_notes','list','1',1)",
                (account,),
            )
            await db.execute(
                "INSERT INTO sessions(id,account_id,token,expires_at) VALUES (?,?,?,9999999999)",
                ("session-" + account, account, "synthetic-token-" + account),
            )
        await db.execute(
            "INSERT INTO journal_entries(id,account_id,tool_name,arguments,permission_decision,outcome) VALUES ('legacy-action','acc_sri_owner','synthetic','{}','allowed','{\"success\":true}')"
        )
        await db.execute(
            "INSERT INTO journal_events(account_id,journal_id,data) VALUES ('acc_sri_owner','legacy-action','{\"account_id\":\"acc_sri_owner\",\"historical\":true}')"
        )
        await db.execute(
            "INSERT INTO tool_operations(account_id,operation_id,tool_name,tool_version,arguments_digest,status,result,run_id,journal_id) VALUES ('acc_sri_owner','legacy-tool-op','synthetic','1','synthetic','succeeded','{\"historical\":true}','legacy-run','legacy-action')"
        )
        await db.commit()


def arguments(owner, root):
    key = root / "key"
    keygen(key)
    plan = inspect_plan(settings.database_path, "acc_sri_owner", owner)
    assert not plan["blockers"]
    return [
        settings.database_path,
        "acc_sri_owner",
        owner,
        plan["fingerprint_sha256"],
        POLICY_VERSION,
        root / "before.arbk",
        key,
    ]


@pytest.mark.asyncio
async def test_transfer_composite_ownership_quarantine_audit_and_real_restore(tmp_path, monkeypatch):
    owner = await seed()
    await rich_seed(owner)
    monkeypatch.setattr(settings, "approved_files_root", tmp_path / "approved")
    with tempfile.TemporaryDirectory(prefix="archer-migration-test-") as directory:
        root = Path(directory)
        args = arguments(owner, root)
        result = apply_plan(*args, offline=True)
        assert result["success"]
        async with get_db() as db:
            assert not await (await db.execute("PRAGMA foreign_key_check")).fetchall()
            for table in [
                "conversations",
                "messages",
                "runs",
                "run_events",
                "notes",
                "note_revisions",
                "memories",
                "memory_evidence",
                "memory_jobs",
                "tool_operations",
                "journal_entries",
                "journal_events",
            ]:
                assert (
                    await (
                        await db.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id=?", ("acc_sri_owner",))
                    ).fetchone()
                )[0] == 0
            assert (await (await db.execute("SELECT content FROM notes WHERE id='owner-note'")).fetchone())[
                0
            ] == "Owner content"
            assert (await (await db.execute("SELECT COUNT(*) FROM sessions")).fetchone())[0] == 0
            assert (await (await db.execute("SELECT COUNT(*) FROM tool_grants WHERE enabled=1")).fetchone())[0] == 0
            legacy = await (
                await db.execute("SELECT subject_key,value,origin FROM memories WHERE id='legacy-memory'")
            ).fetchone()
            assert tuple(legacy) == (None, None, "legacy")
            assert (
                await (
                    await db.execute("SELECT COUNT(*) FROM memory_suppressions WHERE account_id=?", (owner,))
                ).fetchone()
            )[0] == 1
            assert (
                await (await db.execute("SELECT status FROM memory_jobs WHERE message_id='legacy-message'")).fetchone()
            )[0] == "invalidated"
            settings_row = await (
                await db.execute(
                    "SELECT learning_enabled,retrieval_enabled FROM memory_settings WHERE account_id=?", (owner,)
                )
            ).fetchone()
            assert tuple(settings_row) == (0, 0)
            historical = (
                await (await db.execute("SELECT data FROM journal_events WHERE journal_id='legacy-action'")).fetchone()
            )[0]
            assert json.loads(historical) == {"account_id": "acc_sri_owner", "historical": True}
            assert (
                await (
                    await db.execute("SELECT COUNT(*) FROM journal_entries WHERE tool_name='legacy_migration'")
                ).fetchone()
            )[0] == 1
        assert await extractor.preferences_for_context(owner) == []
        assert restore(root / "before.arbk", root / "restored", root / "key")
        with sqlite3.connect(root / "restored" / "archer.db") as db:
            assert (
                db.execute("SELECT account_id FROM notes WHERE id='legacy-plan-note'").fetchone()[0] == "acc_sri_owner"
            )
            assert db.execute("SELECT content FROM notes WHERE id='owner-note'").fetchone()[0] == "Owner content"
            assert db.execute("PRAGMA foreign_key_check").fetchone() is None


@pytest.mark.asyncio
async def test_stale_plan_live_worker_and_audit_failure_rollback(tmp_path, monkeypatch):
    owner = await seed()
    monkeypatch.setattr(settings, "approved_files_root", tmp_path / "approved")
    with tempfile.TemporaryDirectory(prefix="archer-migration-test-") as directory:
        args = arguments(owner, Path(directory))
        before = dump()
        with Worker():
            with pytest.raises(RuntimeError, match="worker holds"):
                apply_plan(*args, offline=True)
        with pytest.raises(ValueError, match="Blocked or stale"):
            apply_plan(*[*args[:3], "0" * 64, *args[4:]], offline=True)
        assert dump() == before
        from app import legacy_migration

        original = legacy_migration._record_transfer

        def fail_after_audit(*values):
            original(*values)
            raise RuntimeError("Synthetic post-write audit failure")

        monkeypatch.setattr(legacy_migration, "_record_transfer", fail_after_audit)
        with pytest.raises(RuntimeError, match="Synthetic post-write"):
            apply_plan(*args, offline=True)
        assert dump() == before
        assert (Path(directory) / "before.arbk").is_file()
