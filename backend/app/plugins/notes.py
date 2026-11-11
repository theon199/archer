"""Owned note CRUD with revision checks; the registry owns the transaction."""

import json
import uuid
from typing import Any

from app.plugins.arguments import NotesArguments
from app.plugins.base import Plugin


class PersonalNotesPlugin(Plugin):
    name = "personal_notes"
    description = (
        "Manage personal notes. Updates/deletions require the current expected_revision. History is inspectable."
    )

    @property
    def schema(self):
        return NotesArguments.model_json_schema()

    async def execute(self, account_id: str, arguments: dict[str, Any]):
        # Keep the public plugin entry point behind the same policy boundary.
        from app.plugins.registry import registry

        return await registry.dispatch(self.name, account_id, arguments)

    async def in_transaction(self, db, account_id, args):
        action = args["action"]
        if action == "list":
            rows = await (
                await db.execute(
                    "SELECT * FROM notes WHERE account_id = ? AND deleted_at IS NULL ORDER BY rowid DESC LIMIT 200",
                    (account_id,),
                )
            ).fetchall()
            return {"success": True, "notes": [self.note(row) for row in rows]}
        row = None
        if action != "create":
            row = await (
                await db.execute("SELECT * FROM notes WHERE id = ? AND account_id = ?", (args.get("id"), account_id))
            ).fetchone()
            if row is None or (row["deleted_at"] and action != "history"):
                return {"success": False, "status": "not_found", "error": "Note not found"}
        if action == "get":
            return {"success": True, "note": self.note(row)}
        if action == "history":
            rows = await (
                await db.execute(
                    "SELECT * FROM note_revisions WHERE note_id = ? AND account_id = ? ORDER BY revision",
                    (row["id"], account_id),
                )
            ).fetchall()
            return {"success": True, "revisions": [dict(r) for r in rows]}
        if action == "create":
            note_id = uuid.uuid4().hex
            await db.execute(
                "INSERT INTO notes (id, account_id, title, content, tags) VALUES (?, ?, ?, ?, ?)",
                (
                    note_id,
                    account_id,
                    args.get("title") or "Untitled Note",
                    args.get("content") or "",
                    json.dumps(args.get("tags") or []),
                ),
            )
        else:
            note_id = row["id"]
            if args["expected_revision"] != row["revision"]:
                return {
                    "success": False,
                    "status": "conflict",
                    "error": "Note revision changed",
                    "current_revision": row["revision"],
                }
            await db.execute(
                "UPDATE notes SET title = COALESCE(?,title), content = COALESCE(?,content), tags = COALESCE(?,tags), revision = revision + 1, deleted_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND account_id = ?",
                (
                    args.get("title"),
                    args.get("content"),
                    json.dumps(args["tags"]) if args.get("tags") is not None else None,
                    action == "delete",
                    note_id,
                    account_id,
                ),
            )
        current = await (
            await db.execute("SELECT * FROM notes WHERE id = ? AND account_id = ?", (note_id, account_id))
        ).fetchone()
        await db.execute(
            "INSERT INTO note_revisions (note_id, account_id, revision, title, content, tags, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                note_id,
                account_id,
                current["revision"],
                current["title"],
                current["content"],
                current["tags"],
                current["deleted_at"],
            ),
        )
        return {"success": True, "note_id": note_id, "revision": current["revision"], "deleted": action == "delete"}

    @staticmethod
    def note(row):
        return {**dict(row), "tags": json.loads(row["tags"])}
