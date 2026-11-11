"""Owner-selected defaults must not overwrite revocations or authorize other accounts."""

from types import SimpleNamespace

import pytest

from app.auth import provision_owner
from app.config import settings
from app.plugins.registry import ACTIONS, DEFAULT_OWNER_ACTIONS, registry, sync_owner_default_grants
from app.storage.database import get_db


@pytest.mark.asyncio
async def test_defaults_enable_only_selected_owner_actions_and_preserve_revocations(monkeypatch):
    monkeypatch.setattr(settings, "tools_default_enabled", True)
    monkeypatch.setitem(
        registry._plugins,
        "mcp_time_current",
        SimpleNamespace(name="mcp_time_current", account_id="acc_sri_owner", version="clock-v1"),
    )
    monkeypatch.setitem(ACTIONS, "mcp_time_current", {"invoke"})
    monkeypatch.setitem(
        registry._plugins, "mcp_future", SimpleNamespace(name="mcp_future", account_id="acc_sri_owner", version="1")
    )
    monkeypatch.setitem(ACTIONS, "mcp_future", {"invoke"})
    async with get_db() as db:
        await db.execute("UPDATE accounts SET is_owner=1 WHERE id='acc_sri_owner'")
        await db.execute("INSERT INTO accounts(id,username,password_hash) VALUES ('other','other','invalid')")
        await db.execute(
            "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES "
            "('acc_sri_owner','personal_notes','delete','1',0),"
            "('acc_sri_owner','web_search','search','old-brave',1)"
        )
        await db.commit()
    await sync_owner_default_grants()
    async with get_db() as db:
        rows = await (await db.execute("SELECT * FROM tool_grants")).fetchall()
        grants = {(row["tool_name"], row["action"]): (row["enabled"], row["tool_version"]) for row in rows}
        for name, actions in DEFAULT_OWNER_ACTIONS.items():
            for action in actions:
                assert (name, action) in grants
        assert grants[("personal_notes", "delete")] == (0, "1")
        assert grants[("web_search", "search")] == (1, "2-tavily")
        assert grants[("mcp_time_current", "invoke")] == (1, "clock-v1")
        assert ("mcp_future", "invoke") not in grants
        assert all(row["account_id"] == "acc_sri_owner" for row in rows)
        count = (await (await db.execute("SELECT COUNT(*) FROM journal_entries")).fetchone())[0]
    await sync_owner_default_grants()
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM journal_entries")).fetchone())[0] == count
        await db.execute("UPDATE tool_grants SET enabled=0 WHERE tool_name='web_fetch' AND action='fetch'")
        await db.commit()
    monkeypatch.setattr(registry.get("mcp_time_current"), "version", "clock-v2")
    await sync_owner_default_grants()
    async with get_db() as db:
        assert (await (await db.execute("SELECT enabled FROM tool_grants WHERE tool_name='web_fetch'")).fetchone())[
            0
        ] == 0
        assert (
            await (
                await db.execute("SELECT tool_version FROM tool_grants WHERE tool_name='mcp_time_current'")
            ).fetchone()
        )[0] == "clock-v1"
    assert (await registry.dispatch("personal_notes", "acc_sri_owner", {"action": "create"}))["success"]
    assert (await registry.dispatch("personal_notes", "other", {"action": "list"}))["status"] == "denied"


@pytest.mark.asyncio
async def test_new_owner_receives_defaults_during_provisioning(monkeypatch):
    monkeypatch.setattr(settings, "tools_default_enabled", True)
    await provision_owner("default-owner", "synthetic-password-for-testing")
    async with get_db() as db:
        rows = await (
            await db.execute(
                "SELECT tool_name,action,enabled FROM tool_grants JOIN accounts ON accounts.id=tool_grants.account_id WHERE accounts.is_owner=1"
            )
        ).fetchall()
    assert any(row["tool_name"] == "web_search" and row["action"] == "search" and row["enabled"] for row in rows)
    assert any(row["tool_name"] == "personal_notes" and row["action"] == "delete" and row["enabled"] for row in rows)
