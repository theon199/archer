"""Real SQLite/worker locks; Docker replies are explicit deterministic unit stubs."""

import json

import pytest

from app.plugins import mcp_connector
from app.plugins.mcp_ownership import reserve
from app.runs.worker import Worker
from app.storage.database import get_db

ACCOUNT = "acc_sri_owner"
IMAGE = "sha256:" + "a" * 64


async def operation():
    async with get_db() as db:
        await db.execute(
            "INSERT INTO tool_operations (account_id,operation_id,tool_name,tool_version,arguments_digest,status,journal_id) VALUES (?,'owned-operation','mcp_unit','1','test-only','running','unit-journal')",
            (ACCOUNT,),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_reservation_requires_lock_and_is_durable_before_launch():
    await operation()
    name = "archer-mcp-" + "b" * 32
    with pytest.raises(RuntimeError, match="worker lock"):
        await reserve(name, "c" * 32, ACCOUNT, "owned-operation", IMAGE)
    with Worker() as worker:
        resource = await reserve(name, "c" * 32, ACCOUNT, "owned-operation", IMAGE)
        async with get_db() as db:
            row = await (await db.execute("SELECT * FROM mcp_resources WHERE name=?", (name,))).fetchone()
        assert row["identity_token"] == resource["identity_token"] and row["worker_owner"] == worker.owner
        with pytest.raises(RuntimeError, match="Another Archer worker"):
            with Worker():
                pass
    with pytest.raises(RuntimeError, match="Worker lock"):
        await worker.recover_resources()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["token", "owner", "image"])
async def test_recovery_refuses_identity_mismatch_and_keeps_record(monkeypatch, changed):
    await operation()
    with Worker() as worker:
        resource = await reserve("archer-mcp-" + "d" * 32, "e" * 32, ACCOUNT, "owned-operation", IMAGE)
        labels = {"archer.resource-token": resource["identity_token"], "archer.worker-owner": resource["worker_owner"]}
        value = {"Id": "f" * 64, "Config": {"Labels": labels, "Image": IMAGE}}
        if changed == "token":
            labels["archer.resource-token"] = "wrong"
        elif changed == "owner":
            labels["archer.worker-owner"] = "wrong"
        else:
            value["Config"]["Image"] = "sha256:" + "0" * 64
        calls = []

        async def controlled(*args):
            calls.append(args)
            return 0, json.dumps([value]).encode()

        monkeypatch.setattr(mcp_connector, "docker_control", controlled)
        with pytest.raises(mcp_connector.MCPResourceCleanupError):
            await worker.recover_resources()
        assert all(call[0] != "rm" for call in calls)
        async with get_db() as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM mcp_resources")).fetchone())[0] == 1


@pytest.mark.asyncio
async def test_verified_recovery_removes_immutable_id_and_journals_without_replay(monkeypatch):
    await operation()
    with Worker() as worker:
        resource = await reserve("archer-mcp-" + "d" * 32, "e" * 32, ACCOUNT, "owned-operation", IMAGE)
        value = {
            "Id": "f" * 64,
            "Config": {
                "Labels": {
                    "archer.resource-token": resource["identity_token"],
                    "archer.worker-owner": resource["worker_owner"],
                },
                "Image": IMAGE,
            },
        }
        calls = []

        async def controlled(*args):
            calls.append(args)
            return (0, json.dumps([value]).encode()) if args[:2] == ("container", "inspect") else (0, b"")

        monkeypatch.setattr(mcp_connector, "docker_control", controlled)
        assert await worker.recover_resources() == 1
        assert ("rm", "-f", "f" * 64) in calls
        async with get_db() as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM mcp_resources")).fetchone())[0] == 0
            assert (await (await db.execute("SELECT status FROM tool_operations")).fetchone())[
                0
            ] == "needs_reconciliation"
            assert (
                json.loads(
                    (
                        await (
                            await db.execute(
                                "SELECT outcome FROM journal_entries WHERE tool_name='mcp_resource_recovery'"
                            )
                        ).fetchone()
                    )[0]
                )["status"]
                == "resource_recovered"
            )


@pytest.mark.asyncio
async def test_unreconciled_resources_are_bounded():
    await operation()
    with Worker():
        for index in range(32):
            await reserve("archer-mcp-" + f"{index:032x}", "e" * 32, ACCOUNT, "owned-operation", IMAGE)
        with pytest.raises(RuntimeError, match="limit"):
            await reserve("archer-mcp-" + "f" * 32, "e" * 32, ACCOUNT, "owned-operation", IMAGE)


@pytest.mark.asyncio
async def test_crash_before_resource_reservation_marks_committed_intent_uncertain():
    from app.plugins.registry import record
    from app.runs.service import append_event, claim, get_run, recover, submit_run

    with Worker() as worker:
        submitted = await submit_run(ACCOUNT, "Synthetic pre-resource crash", None, "local-ollama", "pre-resource")
        run = await claim(worker.owner)
        assert run["id"] == submitted["id"]
        await append_event(run, "tool_call", {"tool": "mcp_unit", "arguments": {}})
        async with get_db() as db:
            entry = await record(
                db,
                ACCOUNT,
                "mcp_unit",
                {},
                "allowed",
                {"success": False, "status": "running"},
                run["id"],
                run["conversation_id"],
            )
            await db.execute(
                "INSERT INTO tool_operations (account_id,operation_id,tool_name,tool_version,arguments_digest,status,run_id,journal_id) VALUES (?,'pre-resource','mcp_unit','1','unit-only','running',?,?)",
                (ACCOUNT, run["id"], entry),
            )
            await db.commit()
        await recover()
        assert (await get_run(ACCOUNT, run["id"]))["status"] == "needs_reconciliation"
        async with get_db() as db:
            row = await (
                await db.execute(
                    "SELECT o.status,j.outcome FROM tool_operations o JOIN journal_entries j ON j.id=o.journal_id WHERE o.operation_id='pre-resource'"
                )
            ).fetchone()
            assert (
                row["status"] == "needs_reconciliation"
                and json.loads(row["outcome"])["status"] == "needs_reconciliation"
            )
            assert (await (await db.execute("SELECT COUNT(*) FROM mcp_resources")).fetchone())[0] == 0
