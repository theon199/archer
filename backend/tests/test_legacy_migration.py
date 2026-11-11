"""Actual SQLite/read-only subprocess plans with explicitly synthetic legacy data."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from app.auth import provision_owner
from app.config import settings
from app.legacy_migration import inspect_plan
from app.storage.database import get_db


async def seed():
    async with get_db() as db:
        await db.execute("UPDATE accounts SET password_hash='local_dev_bypass'")
        await db.execute(
            "INSERT INTO notes(id,account_id,title,content) VALUES ('legacy-plan-note','acc_sri_owner','Synthetic legacy title','SYNTHETIC_PRIVATE_NOTE')"
        )
        await db.commit()
    await provision_owner("synthetic-plan-owner", "synthetic-plan-password")
    async with get_db() as db:
        return (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]


def dump():
    with sqlite3.connect(settings.database_path) as db:
        return list(db.iterdump())


@pytest.mark.asyncio
async def test_read_only_plan_fingerprint_and_no_content_disclosure():
    owner = await seed()
    before = dump()
    plan = inspect_plan(settings.database_path, "acc_sri_owner", owner)
    assert plan["ready_for_review"] and plan["apply_supported"]
    assert plan["counts"]["notes"] == {"source": 1, "target": 0}
    assert plan == inspect_plan(settings.database_path, "acc_sri_owner", owner)
    assert "SYNTHETIC_PRIVATE_NOTE" not in json.dumps(plan) and "argon2id" not in json.dumps(plan)
    assert dump() == before
    async with get_db() as db:
        await db.execute("UPDATE notes SET content='changed synthetic content'")
        await db.commit()
    assert (
        inspect_plan(settings.database_path, "acc_sri_owner", owner)["fingerprint_sha256"] != plan["fingerprint_sha256"]
    )


@pytest.mark.asyncio
async def test_unknown_scoped_schema_and_owner_identity_block():
    owner = await seed()
    async with get_db() as db:
        await db.execute("CREATE TABLE future_capability(account_id TEXT,secret TEXT)")
        await db.commit()
    plan = inspect_plan(settings.database_path, "acc_sri_owner", owner)
    assert "account_scoped_schema_requires_review" in plan["blockers"]
    assert not inspect_plan(settings.database_path, owner, "acc_sri_owner")["ready_for_review"]


@pytest.mark.asyncio
async def test_real_cli_plan_and_unguarded_apply_is_rejected():
    owner = await seed()
    before = dump()
    env = dict(os.environ, ARCHER_DATA_DIR=str(settings.data_dir))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.legacy_migration",
            "plan",
            "--source-account",
            "acc_sri_owner",
            "--target-owner",
            owner,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["mode"] == "read_only_plan"
    assert "SYNTHETIC_PRIVATE_NOTE" not in result.stdout
    apply = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.legacy_migration",
            "apply",
            "--source-account",
            "acc_sri_owner",
            "--target-owner",
            owner,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert apply.returncode == 2 and dump() == before


def test_missing_database_not_created(tmp_path):
    missing = tmp_path / "missing" / "archer.db"
    with pytest.raises(FileNotFoundError):
        inspect_plan(missing, "source", "target")
    assert not missing.parent.exists()


@pytest.mark.asyncio
async def test_idempotency_collisions_and_pending_resources_are_blocked():
    owner = await seed()
    async with get_db() as db:
        for index, account in enumerate(["acc_sri_owner", owner]):
            conversation = f"synthetic-migration-{index}"
            await db.execute(
                "INSERT INTO conversations(id,account_id,title,provider) VALUES (?,?,'Synthetic','local-ollama')",
                (conversation, account),
            )
            await db.execute(
                "INSERT INTO runs(id,account_id,conversation_id,client_operation_id,request_digest,message,provider,status) VALUES (?,?,?,'colliding-key','synthetic','Synthetic','local-ollama','failed')",
                (f"synthetic-run-{index}", account, conversation),
            )
            await db.execute(
                "INSERT INTO tool_operations(account_id,operation_id,tool_name,tool_version,arguments_digest,status,journal_id) VALUES (?,'colliding-operation','synthetic','1','synthetic','needs_reconciliation','synthetic-journal')",
                (account,),
            )
        await db.execute(
            "INSERT INTO mcp_resources(name,identity_token,account_id,operation_id,image_reference,worker_owner) VALUES ('synthetic-unlaunched','synthetic',?,'colliding-operation','synthetic-not-an-image','synthetic')",
            (owner,),
        )
        await db.commit()
    before = dump()
    blockers = inspect_plan(settings.database_path, "acc_sri_owner", owner)["blockers"]
    assert {
        "run_operation_key_collision",
        "tool_operation_key_collision",
        "pending_mcp_resources_require_review",
        "unsettled_tool_outcomes_require_review",
    } <= set(blockers)
    assert dump() == before


@pytest.mark.asyncio
async def test_oversized_legacy_row_refused_without_mutation():
    owner = await seed()
    async with get_db() as db:
        await db.execute("UPDATE notes SET content=?", ("x" * 1048577,))
        await db.commit()
    with pytest.raises(ValueError, match="row-size budget"):
        inspect_plan(settings.database_path, "acc_sri_owner", owner)
