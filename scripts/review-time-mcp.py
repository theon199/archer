#!/usr/bin/env python3
"""Discover and review the exact official time image under Archer's fixed sandbox."""

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


async def review(image, evidence):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters

    from app.plugins.mcp_connector import container_command, digest, docker_control, remove_owned_container
    from app.plugins.mcp_transport import bounded_stdio_client

    name = "archer-mcp-" + uuid.uuid4().hex
    identity = {
        "identity_token": uuid.uuid4().hex,
        "worker_owner": "review-" + uuid.uuid4().hex,
        "image_reference": image,
    }
    command = container_command(image, name, identity)
    params = StdioServerParameters(command=command[0], args=command[1:], env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
    checks = {}
    try:
        with open(os.devnull, "w") as errors:
            async with bounded_stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=8) as session:
                    initialized = await session.initialize()
                    tools = (await session.list_tools()).tools
                    selected = next(tool for tool in tools if tool.name == "get_current_time")
                    advertised = selected.model_dump(mode="json", by_alias=True, exclude_none=True)
                    response = await session.call_tool(
                        "get_current_time", {"timezone": "America/Los_Angeles"}, read_timeout_seconds=8
                    )
                    checks["official_version"] = initialized.server_info.version == "2026.10.19"
                    checks["structured_time_result"] = (
                        not response.is_error and response.structured_content["timezone"] == "America/Los_Angeles"
                    )
                    parsed = datetime.fromisoformat(response.structured_content["datetime"])
                    checks["actual_current_time"] = (
                        abs((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()) < 10
                    )
                    checks["no_referenced_schema"] = "$ref" not in json.dumps(
                        advertised
                    ) and "$dynamicRef" not in json.dumps(advertised)
                    code, raw = await docker_control("container", "inspect", name)
                    if code != 0:
                        raise RuntimeError("Owned container disappeared during review")
                    inspected = json.loads(raw)[0]
                    host = inspected["HostConfig"]
                    checks["network_none"] = host["NetworkMode"] == "none"
                    checks["root_readonly"] = host["ReadonlyRootfs"]
                    checks["no_host_mounts"] = not host["Binds"] and all(
                        m["Type"] == "tmpfs" for m in inspected["Mounts"]
                    )
                    checks["nonroot"] = inspected["Config"]["User"] == "65534:65534"
                    checks["no_ports"] = not host["PortBindings"]
                    checks["bounded_resources"] = (
                        host["Memory"] == 256 * 1024 * 1024
                        and host["PidsLimit"] == 64
                        and host["NanoCpus"] == 1000000000
                    )
                    checks["caps_dropped"] = "ALL" in host["CapDrop"] and any(
                        "no-new-privileges" in s for s in host["SecurityOpt"]
                    )
                    checks["no_credentials"] = {e.split("=", 1)[0] for e in inspected["Config"]["Env"]} <= {
                        "PATH",
                        "LANG",
                        "GPG_KEY",
                        "PYTHON_VERSION",
                        "PYTHON_SHA256",
                        "PYTHONDONTWRITEBYTECODE",
                        "PYTHONUNBUFFERED",
                        "PYTHONTZPATH",
                    }
                    bad = await session.call_tool(
                        "get_current_time", {"timezone": "not/a_timezone"}, read_timeout_seconds=8
                    )
                    checks["invalid_timezone_is_error"] = bad.is_error
    finally:
        await remove_owned_container(name, identity)
    code, raw = await docker_control(
        "container", "ls", "--all", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"
    )
    checks["owned_container_removed"] = code == 0 and not raw.strip()
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "image": image,
        "server_version": initialized.server_info.version,
        "advertised": advertised,
        "advertised_sha256": digest(advertised),
        "time_result": response.structured_content,
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Actual official third-party time server, real Docker fixed private sandbox and SDK structured result. Review discovery only, no model invocation or production data.",
    }
    evidence.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--evidence", type=Path, required=True)
    a = p.parse_args()
    raise SystemExit(asyncio.run(asyncio.wait_for(review(a.image, a.evidence), timeout=40)))
