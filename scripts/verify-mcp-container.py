#!/usr/bin/env python3
"""Real Docker boundary drill with an explicitly synthetic DEVELOPMENT MCP image."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


async def verify(image):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters

    from app.auth import provision_owner
    from app.config import settings
    from app.plugins import mcp_connector as connector
    from app.plugins.mcp_transport import bounded_stdio_client
    from app.plugins.registry import PluginRegistry
    from app.runs.worker import Worker
    from app.storage.database import get_db

    checks = {}
    started = time.monotonic()
    names = []
    tasks = []
    original = connector.container_command
    original_remove = connector.remove_owned_container
    live_before_removal = set()

    def observed_command(image, name, identity=None):
        names.append(name)  # Observe the real random name; do not replace Docker or its response.
        return original(image, name, identity)

    async def observed_remove(name, ownership=None):
        code, output = await connector.docker_control("container", "inspect", name)
        if code == 0 and json.loads(output)[0]["State"]["Running"]:
            live_before_removal.add(name)
        await original_remove(name, ownership)

    connector.container_command = observed_command
    connector.remove_owned_container = observed_remove

    async def inspect_live(name):
        for _ in range(100):
            code, output = await connector.docker_control("container", "inspect", name)
            if code == 0:
                value = json.loads(output)[0]
                if value["State"]["Running"]:
                    return value
            await asyncio.sleep(0.05)
        raise RuntimeError("Owned fixture container never reached running state")

    async def gone(name):
        code, output = await connector.docker_control(
            "container", "ls", "--all", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"
        )
        return code == 0 and not output.strip()

    try:
        image_code, image_output = await connector.docker_control("image", "inspect", image)
        if image_code != 0:
            raise RuntimeError("Reviewed development image must already be local")
        image_info = json.loads(image_output)[0]
        assert image_info["Config"]["Labels"].get("archer.fixture") == "development-mcp-sandbox-only"
        checks["image_exact_id"] = image_info["Id"] == image
        checks["image_arm64"] = image_info["Architecture"] == "arm64"
        discovery = "archer-mcp-" + uuid.uuid4().hex
        command = observed_command(image, discovery)
        params = StdioServerParameters(
            command=command[0], args=command[1:], env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
        )
        try:
            async with bounded_stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=5) as session:
                    await session.initialize()
                    advertised = (
                        (await session.list_tools()).tools[0].model_dump(mode="json", by_alias=True, exclude_none=True)
                    )
                    live = await inspect_live(discovery)
                    checks["presence_query_sees_live_container"] = not await gone(discovery)
                    host = live["HostConfig"]
                    checks["daemon_network_none"] = host["NetworkMode"] == "none"
                    checks["daemon_root_readonly"] = host["ReadonlyRootfs"]
                    checks["daemon_no_host_mounts"] = not host["Binds"] and all(
                        m["Type"] == "tmpfs" for m in live["Mounts"]
                    )
                    checks["daemon_no_public_ports"] = not host["PortBindings"]
                    checks["daemon_unprivileged"] = not host["Privileged"] and live["Config"]["User"] == "65534:65534"
                    checks["daemon_dropped_capabilities"] = "ALL" in host["CapDrop"]
                    checks["daemon_no_new_privileges"] = "no-new-privileges" in host["SecurityOpt"]
                    checks["daemon_resource_limits"] = (
                        host["Memory"] == 268435456 and host["PidsLimit"] == 64 and host["NanoCpus"] == 1000000000
                    )
                    probe = (await session.call_tool("sandbox_inspect", {"mode": "inspect"})).structured_content
                    checks["actual_uid"] = probe["uid"] == 65534
                    checks["actual_readonly_root_write_denied"] = probe["root_write_denied"]
                    checks["actual_tmpfs_writable"] = probe["tmp_writable"]
                    checks["actual_host_loopback_inaccessible"] = probe["network_denied"]
                    checks["actual_host_path_and_socket_absent"] = probe["host_path_absent"] and probe["socket_absent"]
                    checks["actual_no_privileges_capabilities"] = probe["no_new_privs"] and probe["caps_zero"]
                    checks["actual_cgroup_memory_pids_cpu"] = (
                        probe["memory_max"] == "268435456"
                        and probe["pids_max"] == "64"
                        and probe["cpu_max"] == "100000 100000"
                    )
        finally:
            await connector.complete_cleanup(connector.remove_owned_container(discovery))
        checks["discovery_container_removed"] = await gone(discovery)
        with tempfile.TemporaryDirectory(prefix="archer-mcp-container-drill-") as directory:
            settings.data_dir = Path(directory)
            settings.mcp_config_path = None
            settings.env = "production"  # Exercise image profile, never native dev-command exemption.
            await provision_owner("synthetic-container-owner", "synthetic-container-password-only")
            async with get_db() as db:
                account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
                await db.execute(
                    "INSERT INTO accounts (id,username,password_hash) VALUES ('synthetic-other','synthetic-other','test-only')"
                )
                await db.commit()
            with Worker():
                mapping = connector.Mapping(
                    name="mcp_development_container_probe",
                    account_id=account,
                    description="Synthetic DEVELOPMENT fixture for container verification only",
                    remote_tool="sandbox_inspect",
                    input_schema=advertised["inputSchema"],
                    output_schema=advertised["outputSchema"],
                    advertised_sha256=connector.digest(advertised),
                    image=image,
                    read_only=True,
                )
                registry = PluginRegistry()
                registry.register_mcp(mapping)
                os.environ["ARCHER_TEST_AMBIENT_SECRET"] = "SYNTHETIC_SECRET_DO_NOT_LOG"
                count = len(names)
                checks["missing_grant_denied_without_launch"] = (
                    await registry.dispatch(mapping.name, account, {"mode": "inspect"})
                )["status"] == "denied" and len(names) == count
                async with get_db() as db:
                    for owner in [account, "synthetic-other"]:
                        await db.execute(
                            "INSERT INTO tool_grants (account_id,tool_name,action,tool_version,enabled) VALUES (?,?,'invoke',?,1)",
                            (owner, mapping.name, registry.version(mapping.name)),
                        )
                    await db.commit()
                checks["other_account_denied_without_launch"] = (
                    await registry.dispatch(mapping.name, "synthetic-other", {"mode": "inspect"})
                )["status"] == "denied" and len(names) == count
                success = await registry.dispatch(
                    mapping.name, account, {"mode": "inspect"}, operation_id="sandbox-success"
                )
                checks["registry_real_container_success"] = success.get("success") is True
                checks["ambient_secret_not_forwarded"] = success.get("data", {}).get("ambient_secret") == "absent"
                checks["success_container_removed"] = await gone(names[-1])
                count = len(names)
                checks["durable_replay_without_launch"] = (
                    await registry.dispatch(mapping.name, account, {"mode": "inspect"}, operation_id="sandbox-success")
                    == success
                    and len(names) == count
                )
                timeout = await registry.dispatch(
                    mapping.name, account, {"mode": "hold"}, operation_id="sandbox-timeout"
                )
                checks["real_request_timeout_failed"] = timeout["status"] == "failed"
                checks["timeout_container_removed"] = await gone(names[-1])
                count = len(names)
                task = asyncio.create_task(
                    registry.dispatch(mapping.name, account, {"mode": "descendant"}, operation_id="sandbox-cancel")
                )
                for _ in range(100):
                    if len(names) > count:
                        break
                    await asyncio.sleep(0.02)
                tasks.append(task)
                cancel_name = names[-1]
                await inspect_live(cancel_name)
                # Wait for the real descendant tool call, not just container startup.
                host_pids = []
                for _ in range(100):
                    code, output = await connector.docker_control("top", cancel_name, "-eo", "pid,args")
                    if code == 0:
                        lines = output.decode().splitlines()[1:]
                        if any("trap" in line for line in lines):
                            host_pids = [int(line.split()[0]) for line in lines]
                            break
                    await asyncio.sleep(0.05)
                checks["detached_descendant_observed"] = len(host_pids) >= 2
                task.cancel()
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await asyncio.wait_for(task, 15)
                    checks["cancellation_propagated"] = False
                except asyncio.CancelledError:
                    checks["cancellation_propagated"] = True
                checks["cancel_container_removed"] = await gone(cancel_name)
                for _ in range(100):
                    if all(not Path(f"/proc/{pid}").exists() for pid in host_pids):
                        break
                    await asyncio.sleep(0.02)
                checks["all_observed_container_processes_gone"] = bool(host_pids) and all(
                    not Path(f"/proc/{pid}").exists() for pid in host_pids
                )
                async with get_db() as db:
                    canceled = await (
                        await db.execute("SELECT status FROM tool_operations WHERE operation_id='sandbox-cancel'")
                    ).fetchone()
                    checks["cancellation_journal_durable"] = canceled["status"] == "canceled"
                    rows = await (await db.execute("SELECT arguments,outcome,error FROM journal_entries")).fetchall()
                    checks["journal_secret_redacted"] = "SYNTHETIC_SECRET" not in json.dumps(
                        [dict(row) for row in rows]
                    )
        checks["all_owned_containers_absent"] = all([await gone(name) for name in names])
        checks["force_cleanup_reached_live_stubborn_roots"] = set(names) <= live_before_removal
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        connector.container_command = original
        connector.remove_owned_container = original_remove
        for name in names:
            await connector.complete_cleanup(original_remove(name))
    fixtures = ROOT / "backend/tests/fixtures/mcp_container"
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": all(checks.values()),
        "checks": checks,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "architecture": platform.machine(),
        "fixture_image_id": image,
        "base_manifest": "node@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205",
        "advertised_sha256": connector.digest(advertised),
        "probe": probe,
        "owned_container_count": len(names),
        "fixture_files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in fixtures.iterdir() if p.is_file()
        },
        "limitations": [
            "Development fixture, not a third-party connector",
            "Host power-loss behavior not tested; isolated SIGKILL/restart is covered by the separate crash drill",
            "Cgroup limits inspected, not exhaustion stress tests",
            "No real-model MCP selection or browser demonstration",
        ],
        "scope": "Real Docker and MCP SDK production launch profile on synthetic DEVELOPMENT fixture. No third-party account connector, real model, browser or production owner data; no image pull/network build.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        result = asyncio.run(verify(arguments.image))
    except Exception as error:
        result = {
            "passed": False,
            "checked_at": datetime.now(UTC).isoformat(),
            "error_type": type(error).__name__,
            "scope": "Synthetic development container drill failed; no integration success claimed",
        }
    arguments.evidence.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
