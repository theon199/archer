#!/usr/bin/env python3
"""Actual official time MCP + existing local Ollama via registry, synthetic owner."""

import asyncio
import json
import logging
import secrets
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
EVIDENCE = ROOT / "docs/evidence/checkpoint-24-time-integration.json"
DIGEST = "08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68"


async def main():
    from app.auth import provision_owner
    from app.config import settings
    from app.main import app
    from app.plugins import mcp_connector as connector
    from app.plugins.mcp_connector import Mapping
    from app.plugins.registry import registry
    from app.runs.service import get_run, submit_run
    from app.runs.worker import Worker
    from app.storage.database import get_db

    EVIDENCE.write_text(json.dumps({"passed": False, "state": "in_progress"}) + "\n")
    checks = {}
    names = []
    original_command = connector.container_command

    def observed_command(image, name, identity=None):
        names.append(name)
        return original_command(image, name, identity)

    connector.container_command = observed_command
    with tempfile.TemporaryDirectory(prefix="archer-real-time-mcp-") as directory:
        settings.data_dir = Path(directory)
        settings.model_provider = "local-ollama"
        settings.ollama_base_url = "http://127.0.0.1:11434"
        settings.ollama_model = "gemma4:26b"
        settings.ollama_num_ctx = 4096
        settings.expected_model_digest = DIGEST
        settings.run_timeout_seconds = 90
        password = secrets.token_urlsafe(24)
        await provision_owner("time-mcp-owner", password)
        async with get_db() as db:
            account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
            await db.execute(
                "INSERT INTO accounts(id,username,password_hash) VALUES ('other-time-owner','other-time-owner','test-only-invalid')"
            )
            await db.commit()
        raw = json.loads((ROOT / "config/mcp-time/get-current-time.json.example").read_text())
        raw["account_id"] = account
        mapping = Mapping.model_validate(raw)
        registry.register_mcp(mapping)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post("/api/auth/login", json={"username": "time-mcp-owner", "password": password})
            login.raise_for_status()
            client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
            with Worker() as worker:
                denied = await registry.dispatch(
                    mapping.name, account, {"timezone": "America/Los_Angeles"}, operation_id="default-denied"
                )
                checks["default_denied"] = denied["status"] == "denied"
                checks["exact_version_owner_grant"] = (
                    await client.put(
                        "/api/permissions/" + mapping.name + "/invoke",
                        json={"enabled": True, "expected_tool_version": registry.version(mapping.name)},
                    )
                ).status_code == 200
                actual = await registry.dispatch(
                    mapping.name, account, {"timezone": "America/Los_Angeles"}, operation_id="actual-clock"
                )
                checks["real_registry_time_success"] = actual.get("success") is True
                if not actual.get("success"):
                    raise RuntimeError("Actual time connector dispatch failed")
                time_value = actual["data"]
                parsed = datetime.fromisoformat(time_value["datetime"])
                checks["clock_matches_host_within_10s"] = (
                    abs((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()) < 10
                )
                checks["correct_timezone_and_weekday"] = time_value["timezone"] == "America/Los_Angeles" and time_value[
                    "day_of_week"
                ] == parsed.strftime("%A")
                replay = await registry.dispatch(
                    mapping.name, account, {"timezone": "America/Los_Angeles"}, operation_id="actual-clock"
                )
                checks["durable_same_operation_replay"] = replay == actual
                foreign = await registry.dispatch(
                    mapping.name, "other-time-owner", {"timezone": "UTC"}, operation_id="foreign-denied"
                )
                checks["foreign_mapping_denied"] = foreign["status"] == "denied"
                invalid = await registry.dispatch(
                    mapping.name, account, {"timezone": "../../etc/passwd"}, operation_id="invalid-zone"
                )
                checks["unreviewed_timezone_denied"] = invalid["status"] == "denied"
                plugin = registry.get(mapping.name)
                original = plugin.mapping
                plugin.mapping = original.model_copy(update={"advertised_sha256": "0" * 64})
                try:
                    changed = await registry.dispatch(
                        mapping.name, account, {"timezone": "UTC"}, operation_id="changed-advertisement"
                    )
                    checks["actual_changed_advertisement_denied"] = (
                        changed["status"] == "denied" and not changed["success"]
                    )
                finally:
                    plugin.mapping = original
                async with httpx.AsyncClient(trust_env=False, timeout=5) as metadata:
                    tags = (await metadata.get(settings.ollama_base_url + "/api/tags")).json()
                checks["real_model_digest"] = any(
                    m["name"] == settings.ollama_model and m["digest"] == DIGEST for m in tags["models"]
                )
                if not checks["real_model_digest"]:
                    raise RuntimeError("Local model mismatch")
                run = await submit_run(
                    account,
                    "Use mcp_time_current exactly once with timezone America/Los_Angeles to check the current local date and time before planning errands today. Then answer in one sentence with the actual date in YYYY-MM-DD format, weekday and timezone from that tool. Do not use other tools or save a note.",
                    None,
                    "local-ollama",
                    "model-time-once",
                )
                await worker.run_one()
                final = await get_run(account, run["id"])
                async with get_db() as db:
                    calls = await (
                        await db.execute(
                            "SELECT data FROM run_events WHERE run_id=? AND event='tool_call'", (run["id"],)
                        )
                    ).fetchall()
                    outcomes = await (
                        await db.execute(
                            "SELECT data FROM run_events WHERE run_id=? AND event='tool_result'", (run["id"],)
                        )
                    ).fetchall()
                    journal = await (
                        await db.execute(
                            "SELECT outcome FROM journal_entries WHERE run_id=? AND tool_name=?",
                            (run["id"], mapping.name),
                        )
                    ).fetchall()
                    resources = await (await db.execute("SELECT status FROM mcp_resources")).fetchall()
                    read_journal = await (
                        await db.execute(
                            "SELECT arguments,outcome FROM journal_entries WHERE tool_name=? AND run_id IS NULL",
                            (mapping.name,),
                        )
                    ).fetchall()
                checks["real_model_selected_time_connector"] = (
                    len(calls) == 1 and json.loads(calls[0][0])["tool"] == mapping.name
                )
                model_results = [
                    json.loads(r[0])["result"] for r in outcomes if json.loads(r[0])["tool"] == mapping.name
                ]
                checks["real_model_tool_success"] = bool(model_results) and all(r.get("success") for r in model_results)
                checks["real_model_run_success"] = final["status"] == "succeeded" and bool(final["output"].strip())
                checks["answer_grounded_date_weekday"] = (
                    bool(model_results)
                    and model_results[0]["data"]["datetime"][:10] in final["output"]
                    and model_results[0]["data"]["day_of_week"].lower() in final["output"].lower()
                )
                checks["accurate_model_journal"] = (
                    len(journal) == 1 and json.loads(journal[0][0])["status"] == "succeeded"
                )
                checks["resources_all_removed"] = not resources
                gone = []
                for name in names:
                    code, output = await connector.docker_control(
                        "container", "ls", "--all", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"
                    )
                    gone.append(code == 0 and not output.strip())
                checks["exact_owned_containers_absent"] = bool(names) and all(gone)
                checks["only_expected_container_launches"] = len(names) == 3
                checks["journal_arguments_redacted"] = all("America/" not in r["arguments"] for r in read_journal)
                checks["owner_revoke"] = (
                    await client.put(
                        "/api/permissions/" + mapping.name + "/invoke",
                        json={"enabled": False, "expected_tool_version": registry.version(mapping.name)},
                    )
                ).status_code == 200
                checks["revoked_cached_read_denied"] = (
                    await registry.dispatch(
                        mapping.name, account, {"timezone": "America/Los_Angeles"}, operation_id="actual-clock"
                    )
                )["status"] == "denied"
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "checked_at": datetime.now(UTC).isoformat(),
        "image": mapping.image,
        "mapping_version": plugin.version,
        "model": "gemma4:26b",
        "model_digest": DIGEST,
        "observed_time": time_value,
        "model_output": final["output"],
        "scope": "Actual official third-party time server Docker/SDK/registry/worker/SQLite/auth/journal and real local Ollama tool selection. Synthetic owner only. Advertisement-hash mismatch is explicit review fault injection; server bytes/results/inference never mocked.",
    }
    EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        code = asyncio.run(asyncio.wait_for(main(), timeout=150))
    except Exception as error:  # noqa: BLE001 - Save sanitized integration failure.
        result = {
            "passed": False,
            "error_type": type(error).__name__,
            "checked_at": datetime.now(UTC).isoformat(),
            "scope": "Real time integration drill failed; no fake provider substituted",
        }
        EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        code = 1
    raise SystemExit(code)
