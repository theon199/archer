"""Only the authenticated owner can grant or revoke capabilities; never a tool."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import current_account
from app.plugins.registry import ACTIONS, TOOL_VERSION, record, registry
from app.plugins.web_search import tavily_key
from app.storage.database import get_db

router = APIRouter()


class GrantChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    expected_tool_version: str | None = Field(default=None, min_length=1, max_length=128)


async def owner(account_id: str = Depends(current_account)):
    async with get_db() as db:
        row = await (await db.execute("SELECT is_owner FROM accounts WHERE id=?", (account_id,))).fetchone()
    if not row or not row["is_owner"]:
        raise HTTPException(403, "Owner permission required")
    return account_id


@router.get("/permissions")
async def grants(account_id: str = Depends(owner)):
    async with get_db() as db:
        rows = await (
            await db.execute(
                "SELECT tool_name,action,tool_version,enabled,revision FROM tool_grants WHERE account_id=?",
                (account_id,),
            )
        ).fetchall()
    return {
        "available": {k: sorted(v) for k, v in ACTIONS.items() if registry.available(k, account_id)},
        "tool_version": TOOL_VERSION,
        "capabilities": [
            {
                "name": name,
                "version": registry.version(name),
                "description": registry.get(name).description,
                "actions": sorted(actions),
                "configuration_notice": "Unavailable: Tavily API key is not configured or is not private."
                if name == "web_search" and not tavily_key()
                else None,
            }
            for name, actions in sorted(ACTIONS.items())
            if registry.available(name, account_id)
        ],
        "grants": [dict(r) for r in rows],
    }


@router.put("/permissions/{tool_name}/{action}")
async def change(tool_name: str, action: str, body: GrantChange, account_id: str = Depends(owner)):
    if action not in ACTIONS.get(tool_name, set()) or not registry.available(tool_name, account_id):
        raise HTTPException(404, "Unknown capability; MCP connectors require separate review")
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        if body.expected_tool_version is not None and body.expected_tool_version != registry.version(tool_name):
            raise HTTPException(409, "Tool version changed; refresh permissions before granting")
        await db.execute(
            "INSERT INTO tool_grants (account_id,tool_name,action,tool_version,enabled) VALUES (?,?,?,?,?) ON CONFLICT(account_id,tool_name,action) DO UPDATE SET enabled=excluded.enabled,tool_version=excluded.tool_version,revision=tool_grants.revision+1",
            (account_id, tool_name, action, registry.version(tool_name), body.enabled),
        )
        await record(
            db,
            account_id,
            "permission_change",
            {"action": action, "tool_name": tool_name, "enabled": body.enabled},
            "allowed",
            {"success": True, "status": "granted" if body.enabled else "revoked"},
        )
        await db.commit()
    return {"success": True, "enabled": body.enabled, "tool_version": registry.version(tool_name)}
