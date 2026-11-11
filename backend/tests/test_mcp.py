"""Real SDK stdio to an explicitly synthetic development server. No third-party integration claim."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.config import settings
from app.plugins.arguments import SCHEMAS
from app.plugins.mcp_connector import Mapping, digest, load_mappings
from app.plugins.registry import ACTIONS, PluginRegistry
from app.storage.database import get_db

ACCOUNT = "acc_sri_owner"
SERVER = Path(__file__).parent / "fixtures/mcp_development_server.py"
INPUT = {
    "type": "object",
    "properties": {"value": {"type": "string", "maxLength": 100}},
    "required": ["value"],
    "additionalProperties": False,
}
OUTPUT = {
    "type": "object",
    "properties": {"value": {"type": "string"}, "pid": {"type": "integer"}, "ambient_secret": {"type": "string"}},
    "required": ["value", "pid", "ambient_secret"],
    "additionalProperties": False,
}


async def reviewed(monkeypatch, name="mcp_development_inspect"):
    monkeypatch.setattr(settings, "env", "test")
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    with open(os.devnull, "w") as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                advertised = listing.tools[0].model_dump(mode="json", by_alias=True, exclude_none=True)
    mapping = Mapping(
        name=name,
        account_id=ACCOUNT,
        description="Synthetic test fixture only",
        remote_tool="inspect_value",
        input_schema=INPUT,
        output_schema=OUTPUT,
        advertised_sha256=digest(advertised),
        development_command=[sys.executable, str(SERVER)],
        read_only=True,
    )
    registry = PluginRegistry()
    # Global model-schema tables must be reset after this isolated registry test.
    monkeypatch.setitem(SCHEMAS, name, None)
    monkeypatch.setitem(ACTIONS, name, set())
    registry.register_mcp(mapping)
    return registry, mapping


async def grant(registry, name, account=ACCOUNT, version=None):
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO tool_grants (account_id,tool_name,action,tool_version,enabled) VALUES (?,?,'invoke',?,1)",
            (account, name, version or registry.version(name)),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_real_stdio_permission_isolation_replay_and_journal(monkeypatch):
    registry, mapping = await reviewed(monkeypatch)
    monkeypatch.setenv("ARCHER_TEST_AMBIENT_SECRET", "SYNTHETIC_SECRET_DO_NOT_LOG")
    args = {"value": "synthetic-private-content"}
    assert (await registry.dispatch(mapping.name, ACCOUNT, args))["status"] == "denied"
    await grant(registry, mapping.name)
    result = await registry.dispatch(mapping.name, ACCOUNT, args, operation_id="same-read")
    assert result["success"] and result["untrusted_content"]
    assert result["data"]["ambient_secret"] == "absent"
    assert not Path(f"/proc/{result['data']['pid']}").exists()
    assert await registry.dispatch(mapping.name, ACCOUNT, args, operation_id="same-read") == result
    assert (await registry.dispatch(mapping.name, ACCOUNT, {"value": "different"}, operation_id="same-read"))[
        "status"
    ] == "conflict"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO accounts (id,username,password_hash) VALUES ('other-account','other','test-only')"
        )
        await db.commit()
    await grant(registry, mapping.name, "other-account")
    assert not any(p["name"] == mapping.name for p in registry.list_plugins("other-account"))
    assert (await registry.dispatch(mapping.name, "other-account", args))["status"] == "denied"
    assert (await registry.dispatch(mapping.name, ACCOUNT, {"value": "x", "server": "arbitrary"}))["status"] == "denied"
    async with get_db() as db:
        rows = await (
            await db.execute("SELECT arguments,outcome,error FROM journal_entries WHERE account_id=?", (ACCOUNT,))
        ).fetchall()
        assert "synthetic-private-content" not in json.dumps([dict(row) for row in rows])
        assert any(json.loads(row["outcome"]).get("status") == "succeeded" for row in rows)
        await db.execute("UPDATE tool_grants SET enabled=0 WHERE tool_name=?", (mapping.name,))
        await db.commit()
    assert (await registry.dispatch(mapping.name, ACCOUNT, args))["status"] == "denied"


@pytest.mark.asyncio
async def test_schema_config_changes_stale_grants_failure_and_cancellation(monkeypatch):
    registry, mapping = await reviewed(monkeypatch)
    await grant(registry, mapping.name, version="old-config")
    assert (await registry.dispatch(mapping.name, ACCOUNT, {"value": "x"}))["status"] == "denied"
    await grant(registry, mapping.name)
    registry.get(mapping.name).mapping = mapping.model_copy(update={"advertised_sha256": "0" * 64})
    assert (await registry.dispatch(mapping.name, ACCOUNT, {"value": "x"}))["status"] == "denied"
    registry.get(mapping.name).mapping = mapping
    failure = await registry.dispatch(mapping.name, ACCOUNT, {"value": "failure"})
    assert not failure["success"] and "SYNTHETIC_SECRET" not in json.dumps(failure)
    task = asyncio.create_task(registry.dispatch(mapping.name, ACCOUNT, {"value": "slow"}, operation_id="cancel-read"))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with get_db() as db:
        row = await (await db.execute("SELECT status FROM tool_operations WHERE operation_id='cancel-read'")).fetchone()
        assert row["status"] == "canceled"
    monkeypatch.setattr(settings, "env", "production")
    result = await registry.dispatch(mapping.name, ACCOUNT, {"value": "x"})
    assert not result["success"]


def test_config_requires_private_bounded_reviewed_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("[]")
    path.chmod(0o600)
    assert load_mappings(path) == []
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_mappings(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_mappings(link)
    with pytest.raises(ValueError):
        Mapping(
            name="mcp_bad",
            account_id=ACCOUNT,
            description="test",
            remote_tool="x",
            input_schema=INPUT,
            output_schema=OUTPUT,
            advertised_sha256="0" * 64,
            image="unpinned:latest",
            read_only=True,
        )


@pytest.mark.asyncio
async def test_revocation_during_real_handshake_prevents_call(monkeypatch):
    registry, mapping = await reviewed(monkeypatch)
    await grant(registry, mapping.name)
    original = ClientSession.list_tools

    async def revoke_after_listing(session, *args, **kwargs):
        listing = await original(session, *args, **kwargs)
        async with get_db() as db:
            await db.execute("UPDATE tool_grants SET enabled=0 WHERE tool_name=?", (mapping.name,))
            await db.commit()
        return listing

    monkeypatch.setattr(ClientSession, "list_tools", revoke_after_listing)
    result = await registry.dispatch(mapping.name, ACCOUNT, {"value": "must-not-be-returned"})
    assert result["status"] == "denied" and result["error"] == "Connector grant was revoked"


@pytest.mark.asyncio
async def test_permissions_api_binds_grant_to_config_and_owner(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    registry, mapping = await reviewed(monkeypatch)
    monkeypatch.setattr("app.plugins.permissions.registry", registry)
    async with get_db() as db:
        await db.execute("UPDATE accounts SET is_owner=1 WHERE id=?", (ACCOUNT,))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/permissions/{mapping.name}/invoke"
        assert (await client.put(url, json={"enabled": True})).status_code == 401
        response = await client.put(url, json={"enabled": True}, headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200 and response.json()["tool_version"] == registry.version(mapping.name)
        assert (await registry.dispatch(mapping.name, ACCOUNT, {"value": "synthetic"}))["success"]


def test_sdk_malformed_payload_diagnostics_do_not_leak(caplog):
    from mcp.client.stdio import _parse_line

    _parse_line("SYNTHETIC_SECRET_MALFORMED_PROTOCOL")
    assert "SYNTHETIC_SECRET" not in caplog.text
    assert "server payload redacted" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remove_code,list_code,remaining,should_fail",
    [(1, 0, b"", False), (0, 1, b"", True), (0, 0, b"archer-mcp-still-present", True)],
)
async def test_container_cleanup_requires_confirmed_absence(
    monkeypatch, remove_code, list_code, remaining, should_fail
):
    from app.plugins import mcp_connector

    calls = []

    async def controlled(*arguments):  # Explicit deterministic unit stub, no Docker integration claim.
        calls.append(arguments)
        return (remove_code, b"") if arguments[0] == "rm" else (list_code, remaining)

    monkeypatch.setattr(mcp_connector, "docker_control", controlled)
    name = "archer-mcp-" + "a" * 32
    if should_fail:
        with pytest.raises(RuntimeError, match="Cannot verify"):
            await mcp_connector.remove_owned_container(name)
    else:
        await mcp_connector.remove_owned_container(name)
    assert calls[0] == ("rm", "-f", name)
    with pytest.raises(ValueError):
        await mcp_connector.remove_owned_container("unrelated-container")


@pytest.mark.asyncio
async def test_owned_container_cleanup_cannot_detach_under_repeated_cancellation():
    from app.plugins.mcp_connector import complete_cleanup

    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def controlled():  # Explicit unit lifecycle simulation, no Docker integration claim.
        started.set()
        await release.wait()
        finished.set()

    task = asyncio.create_task(complete_cleanup(controlled()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_immutable_local_image_id_supported_and_mutable_tag_rejected(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    value = dict(
        name="mcp_image",
        account_id=ACCOUNT,
        description="unit-only config",
        remote_tool="inspect",
        input_schema=INPUT,
        output_schema=OUTPUT,
        advertised_sha256="0" * 64,
        read_only=True,
    )
    assert Mapping(**value, image="sha256:" + "a" * 64).image
    with pytest.raises(ValueError):
        Mapping(**value, image="mutable:latest")


@pytest.mark.asyncio
async def test_unverified_cleanup_is_durably_uncertain_not_reported_complete(monkeypatch):
    from app.plugins.mcp_connector import MCPResourceCleanupError

    registry, mapping = await reviewed(monkeypatch)
    await grant(registry, mapping.name)
    calls = 0

    async def uncertain(*args, **kwargs):  # Explicit unit error injection, not a real failed-Docker integration.
        nonlocal calls
        calls += 1
        raise MCPResourceCleanupError("SYNTHETIC_SECRET")

    monkeypatch.setattr(registry.get(mapping.name), "execute", uncertain)
    result = await registry.dispatch(mapping.name, ACCOUNT, {"value": "synthetic"}, operation_id="uncertain-cleanup")
    assert result["status"] == "needs_reconciliation" and not result["success"]
    assert "SYNTHETIC_SECRET" not in json.dumps(result)
    assert (
        await registry.dispatch(mapping.name, ACCOUNT, {"value": "synthetic"}, operation_id="uncertain-cleanup")
        == result
    )
    assert calls == 1
    async with get_db() as db:
        row = await (
            await db.execute(
                "SELECT o.status,j.outcome FROM tool_operations o JOIN journal_entries j ON j.id=o.journal_id WHERE o.operation_id='uncertain-cleanup'"
            )
        ).fetchone()
        assert (
            row["status"] == "needs_reconciliation" and json.loads(row["outcome"])["status"] == "needs_reconciliation"
        )
