"""Durable ownership of exact MCP resources. No scanning or prefix deletion."""

import json
from contextvars import ContextVar

from app.config import settings
from app.storage.database import get_db

worker_scope = ContextVar("mcp_worker_scope", default=None)


def require_worker():
    scope = worker_scope.get()
    if scope is None or scope[1] != str(settings.database_path.resolve()) or scope[2].closed:
        raise RuntimeError("Image connectors require the owning worker lock")
    return scope[0]


async def reserve(name, token, account_id, operation_id, image):
    owner = require_worker()
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        count = (await (await db.execute("SELECT COUNT(*) FROM mcp_resources WHERE status='pending'")).fetchone())[0]
        if count >= 32:
            raise RuntimeError("Unreconciled connector resource limit reached")
        await db.execute(
            "INSERT INTO mcp_resources (name,identity_token,account_id,operation_id,image_reference,worker_owner) VALUES (?,?,?,?,?,?)",
            (name, token, account_id, operation_id, image, owner),
        )
        await db.commit()
    return {
        "name": name,
        "identity_token": token,
        "account_id": account_id,
        "operation_id": operation_id,
        "image_reference": image,
        "worker_owner": owner,
    }


async def release(resource, recovery=False):
    from app.plugins.mcp_connector import remove_owned_container

    await remove_owned_container(resource["name"], resource)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "DELETE FROM mcp_resources WHERE name=? AND identity_token=? AND status='pending'",
            (resource["name"], resource["identity_token"]),
        )
        if recovery:
            from app.plugins.registry import record

            await record(
                db,
                resource["account_id"],
                "mcp_resource_recovery",
                {"action": "remove_verified_orphan"},
                "allowed",
                {"success": True, "status": "resource_recovered"},
            )
        await db.commit()


async def recover_resources():
    require_worker()
    async with get_db() as db:
        resources = await (
            await db.execute("SELECT * FROM mcp_resources WHERE status='pending' ORDER BY created_at,name")
        ).fetchall()
    for row in resources:
        resource = dict(row)
        # Loss of the worker means the original action is uncertain, even if
        # resource cleanup subsequently fails. Never leave a dead action running.
        from app.plugins.registry import record

        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            operation = await (
                await db.execute(
                    "SELECT * FROM tool_operations WHERE account_id=? AND operation_id=?",
                    (resource["account_id"], resource["operation_id"]),
                )
            ).fetchone()
            if operation and operation["result"] is None and operation["status"] == "running":
                await db.execute(
                    "UPDATE tool_operations SET status='needs_reconciliation' WHERE account_id=? AND operation_id=?",
                    (resource["account_id"], resource["operation_id"]),
                )
                old = await (
                    await db.execute(
                        "SELECT outcome FROM journal_entries WHERE id=? AND account_id=?",
                        (operation["journal_id"], resource["account_id"]),
                    )
                ).fetchone()
                result = json.loads(old["outcome"]) if old else {}
                result.update(
                    success=False,
                    status="needs_reconciliation",
                    error="Worker interrupted; original read outcome unknown",
                )
                await record(
                    db,
                    resource["account_id"],
                    operation["tool_name"],
                    {},
                    "allowed",
                    result,
                    operation["run_id"],
                    entry_id=operation["journal_id"],
                )
            await db.commit()
        await release(resource, recovery=True)
    return len(resources)
