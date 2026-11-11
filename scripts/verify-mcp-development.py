#!/usr/bin/env python3
"""Bounded real stdio protocol drill against an explicitly synthetic development server."""

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


async def verify(errors):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from app.auth import provision_owner
    from app.config import settings
    from app.plugins.mcp_connector import Mapping, digest
    from app.plugins.registry import PluginRegistry
    from app.storage.database import get_db

    checks = {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="archer-mcp-development-") as directory:
        settings.data_dir = Path(directory)
        settings.mcp_config_path = None
        settings.env = "test"
        await provision_owner("synthetic-mcp-owner", "synthetic-development-password-only")
        async with get_db() as db:
            account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
            await db.execute(
                "INSERT INTO accounts (id,username,password_hash) VALUES ('synthetic-other','synthetic-other','test-only-invalid')"
            )
            await db.commit()
        server = ROOT / "backend/tests/fixtures/mcp_development_server.py"
        async with (
            stdio_client(
                StdioServerParameters(command=sys.executable, args=[str(server)]),
                errlog=errors,
            ) as (read, write),
            ClientSession(read, write, read_timeout_seconds=5) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()
            advertised = listing.tools[0].model_dump(mode="json", by_alias=True, exclude_none=True)
        mapping = Mapping(
            name="mcp_development_inspect",
            account_id=account,
            description="Synthetic development protocol demonstration only",
            remote_tool="inspect_value",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string", "maxLength": 100}},
                "required": ["value"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "pid": {"type": "integer"},
                    "ambient_secret": {"type": "string"},
                },
                "required": ["value", "pid", "ambient_secret"],
                "additionalProperties": False,
            },
            advertised_sha256=digest(advertised),
            development_command=[sys.executable, str(server)],
            read_only=True,
        )
        registry = PluginRegistry()
        registry.register_mcp(mapping)
        arguments = {"value": "synthetic-private-data"}
        checks["missing_grant_denied"] = (await registry.dispatch(mapping.name, account, arguments))[
            "status"
        ] == "denied"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO tool_grants (account_id,tool_name,action,tool_version,enabled) VALUES (?,?,'invoke',?,1)",
                (account, mapping.name, registry.version(mapping.name)),
            )
            await db.commit()
        os.environ["ARCHER_TEST_AMBIENT_SECRET"] = "SYNTHETIC_SECRET_DO_NOT_LOG"
        result = await registry.dispatch(mapping.name, account, arguments, operation_id="development-read")
        checks["real_stdio_structured_read"] = (
            result.get("success") and result.get("data", {}).get("value") == arguments["value"]
        )
        checks["ambient_secret_absent"] = result.get("data", {}).get("ambient_secret") == "absent"
        checks["owned_server_reaped"] = bool(
            result.get("data", {}).get("pid") and not Path(f"/proc/{result['data']['pid']}").exists()
        )
        checks["idempotent_replay"] = (
            await registry.dispatch(mapping.name, account, arguments, operation_id="development-read") == result
        )
        checks["other_account_denied"] = (await registry.dispatch(mapping.name, "synthetic-other", arguments))[
            "status"
        ] == "denied"
        checks["other_account_listing_hidden"] = not any(
            p["name"] == mapping.name for p in registry.list_plugins("synthetic-other")
        )
        checks["arbitrary_dispatch_args_denied"] = (
            await registry.dispatch(mapping.name, account, {"value": "x", "server": "arbitrary"})
        )["status"] == "denied"
        task = asyncio.create_task(
            registry.dispatch(
                mapping.name,
                account,
                {"value": "slow"},
                operation_id="cancel-development-read",
            )
        )
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            checks["cancellation_propagates"] = True
        async with get_db() as db:
            canceled = await (
                await db.execute("SELECT status FROM tool_operations WHERE operation_id='cancel-development-read'")
            ).fetchone()
            checks["cancellation_durable"] = canceled["status"] == "canceled"
            rows = await (await db.execute("SELECT arguments,outcome,error FROM journal_entries")).fetchall()
            text = json.dumps([dict(row) for row in rows])
            checks["journal_sanitized"] = "synthetic-private-data" not in text and "SYNTHETIC_SECRET" not in text
            checks["journal_configuration_version"] = registry.version(mapping.name) in text
            await db.execute("UPDATE tool_grants SET enabled=0")
            await db.commit()
        checks["revoked_grant_denied"] = (await registry.dispatch(mapping.name, account, arguments))[
            "status"
        ] == "denied"
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": all(checks.values()),
        "checks": checks,
        "sdk": version("mcp"),
        "jsonschema": version("jsonschema"),
        "architecture": platform.machine(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "advertised_sha256": mapping.advertised_sha256,
        "configuration_version": registry.version(mapping.name),
        "scope": "Real SDK stdio and SQLite against explicitly synthetic development server. No real third-party service, production container, browser, model inference or production owner data.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    with open(os.devnull, "w") as errors:
        result = asyncio.run(verify(errors))
    arguments.evidence.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
