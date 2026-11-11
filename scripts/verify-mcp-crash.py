#!/usr/bin/env python3
"""Actual worker-process SIGKILL/restart drill with a synthetic DEVELOPMENT connector."""

import argparse
import asyncio
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


async def verify(image):
    from app.auth import provision_owner
    from app.config import settings
    from app.plugins.mcp_connector import (
        Mapping,
        MCPResourceCleanupError,
        container_command,
        docker_control,
        remove_owned_container,
    )
    from app.storage.database import get_db

    # Reuse the actual fixed-profile discovery/probe; it returns the advertised definition.
    spec = importlib.util.spec_from_file_location("sandbox_drill", ROOT / "scripts/verify-mcp-container.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sandbox = await module.verify(image)
    assert sandbox["passed"]
    # These JSON Schema values are the reviewed development-fixture contract.
    input_schema = {
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": ["inspect", "hold", "descendant"]}},
        "required": ["mode"],
        "additionalProperties": False,
    }
    fields = {
        "uid": "integer",
        "root_write_denied": "boolean",
        "tmp_writable": "boolean",
        "network_denied": "boolean",
        "host_path_absent": "boolean",
        "socket_absent": "boolean",
        "ambient_secret": "string",
        "memory_max": "string",
        "pids_max": "string",
        "cpu_max": "string",
        "no_new_privs": "boolean",
        "caps_zero": "boolean",
        "pid": "integer",
    }
    output_schema = {
        "type": "object",
        "properties": {k: {"type": v} for k, v in fields.items()},
        "required": list(fields),
        "additionalProperties": False,
    }
    checks = {}
    processes = []
    resources = []
    with tempfile.TemporaryDirectory(prefix="archer-mcp-crash-") as directory:
        root = Path(directory)
        settings.data_dir = root / "data"
        settings.mcp_config_path = None
        await provision_owner("synthetic-crash-owner", "synthetic-development-password-only")
        async with get_db() as db:
            account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
        mapping = Mapping(
            name="mcp_development_container_probe",
            account_id=account,
            description="Synthetic DEVELOPMENT crash fixture only",
            remote_tool="sandbox_inspect",
            input_schema=input_schema,
            output_schema=output_schema,
            advertised_sha256=sandbox["advertised_sha256"],
            image=image,
            read_only=True,
        )
        config = root / "reviewed-mcp.json"
        config.write_text(json.dumps([mapping.model_dump()]))
        config.chmod(0o600)
        from app.plugins.mcp_connector import digest

        async with get_db() as db:
            await db.execute(
                "INSERT INTO tool_grants (account_id,tool_name,action,tool_version,enabled) VALUES (?,?,'invoke',?,1)",
                (account, mapping.name, digest(mapping.model_dump())),
            )
            await db.commit()
        environment = dict(
            os.environ,
            ARCHER_DATA_DIR=str(settings.data_dir),
            ARCHER_MCP_CONFIG_PATH=str(config),
            ARCHER_ENV="production",
            ARCHER_HOST="127.0.0.1",
            ARCHER_MODEL_PROVIDER="local-ollama",
        )
        logs = (root / "worker.log").open("w")
        try:
            driver = subprocess.Popen(
                [sys.executable, str(ROOT / "backend/tests/fixtures/mcp_crash_driver.py")],
                cwd=ROOT / "backend",
                env=environment,
                stdout=logs,
                stderr=logs,
            )
            processes.append(driver)
            resource = None
            host_pids = []
            for _ in range(100):
                async with get_db() as db:
                    row = await (await db.execute("SELECT * FROM mcp_resources")).fetchone()
                if row:
                    resource = dict(row)
                    if not resources:
                        resources.append(resource)
                    code, data = await docker_control("top", resource["name"], "-eo", "pid,args")
                    if code == 0 and "trap" in data.decode():
                        host_pids = [int(line.split()[0]) for line in data.decode().splitlines()[1:]]
                        break
                if driver.poll() is not None:
                    raise RuntimeError("Development crash driver exited before crash point")
                await asyncio.sleep(0.03)
            assert resource and host_pids
            checks["durable_record_while_read_active"] = resource["status"] == "pending"
            code, data = await docker_control("container", "inspect", resource["name"])
            live = json.loads(data)[0]
            checks["identity_label_matches_durable_record"] = (
                live["Config"]["Labels"]["archer.resource-token"] == resource["identity_token"]
            )
            checks["detached_descendant_active"] = len(host_pids) >= 2
            driver.send_signal(signal.SIGKILL)
            driver.wait(timeout=5)
            checks["actual_worker_driver_sigkill"] = driver.returncode == -signal.SIGKILL
            code, data = await docker_control("container", "inspect", resource["name"])
            checks["orphan_survives_worker_sigkill"] = code == 0 and json.loads(data)[0]["State"]["Running"]
            replacement = subprocess.Popen(
                [sys.executable, "-m", "app.runs.worker"],
                cwd=ROOT / "backend",
                env=environment,
                stdout=logs,
                stderr=logs,
            )
            processes.append(replacement)
            for _ in range(150):
                async with get_db() as db:
                    pending = (await (await db.execute("SELECT COUNT(*) FROM mcp_resources")).fetchone())[0]
                    status = (await (await db.execute("SELECT status FROM runs")).fetchone())[0]
                    pulse = await (await db.execute("SELECT heartbeat FROM worker_status WHERE id=1")).fetchone()
                if pending == 0 and status == "needs_reconciliation" and pulse:
                    break
                if replacement.poll() is not None:
                    raise RuntimeError("Replacement worker failed recovery")
                await asyncio.sleep(0.03)
            checks["replacement_worker_alive"] = replacement.poll() is None
            checks["resource_ledger_reconciled"] = pending == 0
            checks["run_remains_uncertain"] = status == "needs_reconciliation"
            code, data = await docker_control(
                "container", "ls", "--all", "--filter", f"name=^/{resource['name']}$", "--format", "{{.Names}}"
            )
            checks["exact_orphan_removed"] = code == 0 and not data.strip()
            for _ in range(100):
                if all(not Path(f"/proc/{pid}").exists() for pid in host_pids):
                    break
                await asyncio.sleep(0.01)
            checks["all_observed_orphan_processes_gone"] = all(not Path(f"/proc/{pid}").exists() for pid in host_pids)
            async with get_db() as db:
                op = await (
                    await db.execute(
                        "SELECT status,result FROM tool_operations WHERE operation_id='synthetic-crash-operation'"
                    )
                ).fetchone()
                actions = await (
                    await db.execute("SELECT outcome FROM journal_entries WHERE tool_name='mcp_resource_recovery'")
                ).fetchall()
                checks["original_operation_not_reported_success"] = (
                    op["status"] == "needs_reconciliation" and op["result"] is None
                )
                checks["recovery_action_journaled"] = any(
                    json.loads(a["outcome"])["status"] == "resource_recovered" for a in actions
                )
            await asyncio.sleep(0.2)
            async with get_db() as db:
                count = (await (await db.execute("SELECT COUNT(*) FROM tool_operations")).fetchone())[0]
            checks["interrupted_read_not_reexecuted"] = count == 1
            # A new owned negative-control container simulates a name reused with
            # a different token. Do not touch any unrelated user container.
            from app.plugins.mcp_ownership import reserve
            from app.runs.worker import Worker

            replacement.terminate()
            replacement.wait(timeout=5)
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO tool_operations (account_id,operation_id,tool_name,tool_version,arguments_digest,status,journal_id) VALUES (?,'identity-negative','mcp_development_container_probe','test','test','running','identity-negative-journal')",
                    (account,),
                )
                await db.commit()
            with Worker() as fence:
                recorded = await reserve(
                    "archer-mcp-" + uuid.uuid4().hex, uuid.uuid4().hex, account, "identity-negative", image
                )
                actual = dict(recorded, identity_token=uuid.uuid4().hex)
                resources.append(actual)
                negative = subprocess.Popen(
                    container_command(image, actual["name"], actual),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=logs,
                    env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
                )
                processes.append(negative)
                for _ in range(100):
                    code, data = await docker_control("container", "inspect", actual["name"])
                    if code == 0 and json.loads(data)[0]["State"]["Running"]:
                        break
                    await asyncio.sleep(0.02)
                assert code == 0
                rejected = False
                try:
                    await fence.recover_resources()
                except MCPResourceCleanupError:
                    rejected = True
                checks["real_identity_mismatch_rejected"] = rejected
                code, data = await docker_control("container", "inspect", actual["name"])
                checks["mismatched_container_left_running"] = code == 0 and json.loads(data)[0]["State"]["Running"]
                async with get_db() as db:
                    row = await (
                        await db.execute("SELECT identity_token FROM mcp_resources WHERE name=?", (actual["name"],))
                    ).fetchone()
                    checks["mismatched_record_preserved"] = (
                        row is not None and row["identity_token"] == recorded["identity_token"]
                    )
                # Verifier knows the actual identity of the control it created;
                # remove only that identity, then discard its synthetic ledger row.
                await remove_owned_container(actual["name"], actual)
                async with get_db() as db:
                    await db.execute(
                        "DELETE FROM mcp_resources WHERE name=? AND identity_token=?",
                        (recorded["name"], recorded["identity_token"]),
                    )
                    await db.commit()

        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if process.stdin:
                    process.stdin.close()
            for resource in resources:
                await remove_owned_container(resource["name"], resource)
            logs.close()
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": all(checks.values()),
        "checks": checks,
        "fixture_image_id": image,
        "sandbox_checks_passed": sandbox["passed"],
        "scope": "Actual SIGKILL of owned Worker-lock/tool-execution development process and restart of real app.runs.worker; real Docker/SDK/SQLite, synthetic tool proposal (no inference), no third-party or production owner data.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(verify(args.image))
    except Exception as error:
        result = {
            "passed": False,
            "checked_at": datetime.now(UTC).isoformat(),
            "error_type": type(error).__name__,
            "scope": "Development crash verification failed; no success claimed",
        }
    args.evidence.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
