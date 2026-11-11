"""Actual SQL/auth permission metadata; synthetic mapping metadata never executes."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.plugins.registry import ACTIONS, registry
from app.storage.database import get_db

ACCOUNT = "acc_sri_owner"


@pytest.mark.asyncio
async def test_owner_catalog_versions_isolation_and_stale_change_refusal(monkeypatch):
    async with get_db() as db:
        await db.execute("UPDATE accounts SET is_owner=1 WHERE id=?", (ACCOUNT,))
        await db.execute(
            "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'web_fetch','fetch','old-version',1)",
            (ACCOUNT,),
        )
        await db.commit()
    for name, account in [("mcp_catalog_fixture", ACCOUNT), ("mcp_foreign_catalog", "foreign-owner")]:
        monkeypatch.setitem(
            registry._plugins,
            name,
            SimpleNamespace(
                name=name, version="a" * 64, description="Explicit metadata fixture; never executed", account_id=account
            ),
        )
        monkeypatch.setitem(ACTIONS, name, {"invoke"})
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
    ) as client:
        response = await client.get("/api/permissions")
        assert response.status_code == 200
        body = response.json()
        capabilities = {item["name"]: item for item in body["capabilities"]}
        assert capabilities["web_fetch"]["version"] == "1" and capabilities["web_fetch"]["actions"] == ["fetch"]
        assert capabilities["mcp_catalog_fixture"]["version"] == "a" * 64
        assert "mcp_foreign_catalog" not in capabilities
        assert (
            capabilities["web_search"]["configuration_notice"]
            == "Unavailable: Tavily API key is not configured or is not private."
        )
        assert any(grant["tool_version"] == "old-version" for grant in body["grants"])
        rejected = await client.put(
            "/api/permissions/web_fetch/fetch", json={"enabled": True, "expected_tool_version": "stale-version"}
        )
        assert rejected.status_code == 409
        async with get_db() as db:
            row = await (
                await db.execute("SELECT tool_version,revision FROM tool_grants WHERE tool_name='web_fetch'")
            ).fetchone()
            assert row["tool_version"] == "old-version" and row["revision"] == 1
            assert (await (await db.execute("SELECT COUNT(*) FROM journal_entries")).fetchone())[0] == 0
        granted = await client.put(
            "/api/permissions/web_fetch/fetch", json={"enabled": True, "expected_tool_version": "1"}
        )
        assert granted.status_code == 200
        assert (
            await client.put(
                "/api/permissions/mcp_foreign_catalog/invoke", json={"enabled": True, "expected_tool_version": "a" * 64}
            )
        ).status_code == 404
        async with get_db() as db:
            await db.execute("UPDATE accounts SET is_owner=0 WHERE id=?", (ACCOUNT,))
            await db.commit()
        assert (await client.get("/api/permissions")).status_code == 403
        assert (
            await client.put("/api/permissions/web_fetch/fetch", json={"enabled": False, "expected_tool_version": "1"})
        ).status_code == 403
