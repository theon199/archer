"""Owner tool authorization, idempotent operations and transactional journaling."""

import asyncio
import hashlib
import json
import uuid
from typing import Any

from pydantic import ValidationError

from app.config import settings
from app.journal.service import emit_journal_event
from app.plugins.arguments import SCHEMAS
from app.plugins.base import Plugin
from app.plugins.files import ApprovedFilesPlugin
from app.plugins.mcp_connector import MCPResourceCleanupError, ReviewedMCP, ReviewedSchema, load_mappings
from app.plugins.notes import PersonalNotesPlugin
from app.plugins.web_fetch import WebFetchPlugin
from app.plugins.web_search import WebSearchPlugin
from app.storage.database import get_db

TOOL_VERSION = "1"
ACTIONS = {
    "personal_notes": {"list", "get", "history", "create", "update", "delete"},
    "read_approved_file": {"list", "read"},
    "web_search": {"search"},
    "web_fetch": {"fetch"},
}


def audit_arguments(args):
    # Content is in the owned note/operation, not copied into the action journal.
    return {
        k: v if k in {"action", "id", "expected_revision", "count", "tool_name", "enabled"} else "[redacted]"
        for k, v in args.items()
    }


async def record(db, account_id, name, args, decision, result, run_id=None, conversation_id=None, entry_id=None):
    entry_id = entry_id or "act_" + uuid.uuid4().hex
    summary = {
        k: v
        for k, v in result.items()
        if k
        in {"success", "status", "note_id", "revision", "deleted", "current_revision", "tool_version", "grant_revision"}
    }
    await db.execute(
        "INSERT INTO journal_entries (id, account_id, conversation_id, run_id, tool_name, arguments, permission_decision, outcome, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET outcome=excluded.outcome, error=excluded.error",
        (
            entry_id,
            account_id,
            conversation_id,
            run_id,
            name,
            json.dumps({key: "[redacted]" for key in args} if name.startswith("mcp_") else audit_arguments(args)),
            decision,
            json.dumps(summary),
            result.get("error"),
        ),
    )
    await emit_journal_event(db, entry_id)
    return entry_id


class PluginRegistry:
    def __init__(self):
        self._plugins: dict[str, Plugin] = {}
        for plugin in [ApprovedFilesPlugin(), PersonalNotesPlugin(), WebSearchPlugin(), WebFetchPlugin()]:
            self.register(plugin)
        for mapping in load_mappings(settings.mcp_config_path):
            self.register_mcp(mapping)

    def register(self, plugin):
        self._plugins[plugin.name] = plugin

    def register_mcp(self, mapping):
        if mapping.name in self._plugins:
            raise ValueError("Duplicate reviewed tool")
        self.register(ReviewedMCP(mapping))
        SCHEMAS[mapping.name] = ReviewedSchema(mapping.input_schema)
        ACTIONS[mapping.name] = {"invoke"}

    def version(self, name):
        return getattr(self.get(name), "version", TOOL_VERSION)

    def available(self, name, account_id):
        plugin = self.get(name)
        return plugin is not None and getattr(plugin, "account_id", account_id) == account_id

    def get(self, name):
        return self._plugins.get(name)

    def list_plugins(self, account_id=None):
        return [
            {
                "name": p.name,
                "version": self.version(p.name),
                "description": p.description,
                "schema": SCHEMAS[p.name].model_json_schema(),
            }
            for p in self._plugins.values()
            if p.name in SCHEMAS and self.available(p.name, account_id)
        ]

    def get_ollama_tools(self, account_id=None):
        return [
            {
                "type": "function",
                "function": {"name": p["name"], "description": p["description"], "parameters": p["schema"]},
            }
            for p in self.list_plugins(account_id)
        ]

    async def dispatch(
        self,
        tool_name: str,
        account_id: str,
        arguments: dict[str, Any],
        *,
        operation_id=None,
        run_id=None,
        conversation_id=None,
    ):
        operation_id = operation_id or uuid.uuid4().hex
        name = tool_name if isinstance(tool_name, str) and tool_name in SCHEMAS else "unregistered_tool"
        args = {}
        invalid = name == "unregistered_tool" or not self.available(name, account_id)
        version = self.version(name)
        if not invalid:
            try:
                args = SCHEMAS[name].model_validate(arguments).model_dump(exclude_none=True)
            except (ValidationError, TypeError, ValueError):
                invalid = True
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if invalid:
                result = {
                    "success": False,
                    "status": "denied",
                    "permission_decision": "denied",
                    "error": "Unknown tool or invalid arguments",
                }
                await record(db, account_id, name, {}, "denied", result, run_id, conversation_id)
                await db.commit()
                return result
            action = getattr(self.get(name), "fixed_action", None) or args.get("action", "search")
            grant = await (
                await db.execute(
                    "SELECT * FROM tool_grants WHERE account_id = ? AND tool_name = ? AND action = ?",
                    (account_id, name, action),
                )
            ).fetchone()
            if not grant or not grant["enabled"] or grant["tool_version"] != version:
                result = {
                    "success": False,
                    "status": "denied",
                    "permission_decision": "denied",
                    "error": "No current grant for this tool action",
                }
                await record(db, account_id, name, args, "denied", result, run_id, conversation_id)
                await db.commit()
                return result
            if run_id:
                run = await (
                    await db.execute("SELECT status FROM runs WHERE id = ? AND account_id = ?", (run_id, account_id))
                ).fetchone()
                if run is None or run["status"] != "generating":
                    result = {
                        "success": False,
                        "status": "denied",
                        "permission_decision": "denied",
                        "error": "Run is not active",
                    }
                    await record(db, account_id, name, args, "denied", result, run_id, conversation_id)
                    await db.commit()
                    return result
            digest = hashlib.sha256(
                json.dumps([name, version, args], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            previous = await (
                await db.execute(
                    "SELECT * FROM tool_operations WHERE account_id = ? AND operation_id = ?",
                    (account_id, operation_id),
                )
            ).fetchone()
            if previous:
                if previous["arguments_digest"] != digest:
                    return {
                        "success": False,
                        "status": "conflict",
                        "error": "Operation ID already used for different arguments",
                    }
                if previous["result"]:
                    return json.loads(previous["result"])
                return {
                    "success": False,
                    "status": "needs_reconciliation",
                    "error": "Operation was interrupted; do not repeat blindly",
                }
            entry_id = "act_" + uuid.uuid4().hex
            await db.execute(
                "INSERT INTO tool_operations (account_id, operation_id, tool_name, tool_version, arguments_digest, status, run_id, journal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, operation_id, name, version, digest, "running", run_id, entry_id),
            )
            if name == "personal_notes":
                result = await self.get(name).in_transaction(db, account_id, args)
                result.update(permission_decision="allowed", tool_version=version, grant_revision=grant["revision"])
                result.setdefault("status", "succeeded" if result["success"] else "failed")
                await db.execute(
                    "UPDATE tool_operations SET status=?, result=? WHERE account_id=? AND operation_id=?",
                    (result["status"], json.dumps(result), account_id, operation_id),
                )
                await record(db, account_id, name, args, "allowed", result, run_id, conversation_id, entry_id)
                await db.commit()
                return result
            await record(
                db,
                account_id,
                name,
                args,
                "allowed",
                {"success": False, "status": "running", "tool_version": version, "grant_revision": grant["revision"]},
                run_id,
                conversation_id,
                entry_id,
            )
            await db.commit()
        # Only read-only external operations reach this path. No transaction spans I/O.
        try:
            async with asyncio.timeout(15):
                plugin = self.get(name)
                if isinstance(plugin, ReviewedMCP):
                    result = await plugin.execute(account_id, args, operation_id=operation_id)
                else:
                    result = await plugin.execute(account_id, args)
        except asyncio.CancelledError:
            await self.finish_read(
                account_id, operation_id, {"success": False, "status": "canceled", "error": "Read canceled"}
            )
            raise
        except MCPResourceCleanupError:
            result = {
                "success": False,
                "status": "needs_reconciliation",
                "error": "Connector cleanup unverified; owner review required",
            }
        except Exception:
            result = {"success": False, "status": "failed", "error": "Tool read failed"}
        result.update(permission_decision="allowed", tool_version=version, grant_revision=grant["revision"])
        result.setdefault("status", "succeeded" if result["success"] else "failed")
        await self.finish_read(account_id, operation_id, result)
        return result

    async def finish_read(self, account_id, operation_id, result):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT journal_id FROM tool_operations WHERE account_id=? AND operation_id=?",
                    (account_id, operation_id),
                )
            ).fetchone()
            await db.execute(
                "UPDATE tool_operations SET status=?, result=? WHERE account_id=? AND operation_id=?",
                (result["status"], json.dumps(result), account_id, operation_id),
            )
            summary = {k: v for k, v in result.items() if k in {"success", "status", "tool_version", "grant_revision"}}
            await db.execute(
                "UPDATE journal_entries SET outcome=?, error=? WHERE id=? AND account_id=?",
                (json.dumps(summary), result.get("error"), row["journal_id"], account_id),
            )
            await emit_journal_event(db, row["journal_id"])
            await db.commit()


registry = PluginRegistry()

# These are the specific actions the owner chose to enable by default. New
# connectors or capabilities are not added here automatically.
DEFAULT_OWNER_ACTIONS = {
    "personal_notes": {"create", "delete", "get", "history", "list", "update"},
    "read_approved_file": {"list", "read"},
    "web_search": {"search"},
    "web_fetch": {"fetch"},
    "mcp_time_current": {"invoke"},
}


async def apply_owner_default_grants(db, account_id):
    """Enable selected owner actions without undoing an explicit revoke."""
    if not settings.tools_default_enabled:
        return
    applied = await (
        await db.execute(
            "SELECT 1 FROM journal_entries WHERE account_id=? AND tool_name='owner_default_tool_policy' LIMIT 1",
            (account_id,),
        )
    ).fetchone()
    if applied:
        return
    for name, actions in sorted(DEFAULT_OWNER_ACTIONS.items()):
        if not registry.available(name, account_id):
            continue
        version = registry.version(name)
        for action in sorted(actions & ACTIONS.get(name, set())):
            current = await (
                await db.execute(
                    "SELECT enabled,tool_version FROM tool_grants WHERE account_id=? AND tool_name=? AND action=?",
                    (account_id, name, action),
                )
            ).fetchone()
            if current is None:
                await db.execute(
                    "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,?,?,?,1)",
                    (account_id, name, action, version),
                )
            elif current["enabled"] and current["tool_version"] != version:
                await db.execute(
                    "UPDATE tool_grants SET tool_version=?,revision=revision+1 WHERE account_id=? AND tool_name=? AND action=?",
                    (version, account_id, name, action),
                )
            else:
                continue
            await record(
                db,
                account_id,
                "permission_change",
                {"action": action, "tool_name": name, "enabled": True},
                "allowed",
                {"success": True, "status": "default_enabled", "tool_version": version},
            )
    await record(
        db,
        account_id,
        "owner_default_tool_policy",
        {"action": "apply"},
        "allowed",
        {"success": True, "status": "applied"},
    )


async def sync_owner_default_grants():
    if not settings.tools_default_enabled:
        return
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        owners = await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchall()
        for owner in owners:
            await apply_owner_default_grants(db, owner["id"])
        await db.commit()
