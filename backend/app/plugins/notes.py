"""Personal Notes Plugin - Safe notes CRUD with strict account ownership."""

import json
import uuid
from typing import Any

from app.plugins.base import Plugin
from app.storage.database import get_db


class PersonalNotesPlugin(Plugin):
    """Manage private personal notes for the current user."""

    @property
    def name(self) -> str:
        return "personal_notes"

    @property
    def description(self) -> str:
        return "Create, search, view, update, or delete personal notes."

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "delete"],
                    "description": "Action to perform on personal notes.",
                },
                "id": {"type": "string", "description": "Note identifier (for get/update/delete)."},
                "title": {"type": "string", "description": "Title of the note."},
                "content": {"type": "string", "description": "Markdown body content of the note."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for grouping."},
            },
            "required": ["action"],
        }

    async def execute(self, account_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments.get("action", "list")

        async with get_db() as db:
            if action == "list":
                async with db.execute(
                    "SELECT id, title, tags, created_at, updated_at FROM notes WHERE account_id = ? ORDER BY updated_at DESC",
                    (account_id,),
                ) as cursor:
                    rows = await cursor.fetchall()
                    notes = []
                    for r in rows:
                        notes.append(
                            {
                                "id": r["id"],
                                "title": r["title"],
                                "tags": json.loads(r["tags"]),
                                "created_at": r["created_at"],
                                "updated_at": r["updated_at"],
                            }
                        )
                    return {"success": True, "notes": notes}

            elif action == "get":
                note_id = arguments.get("id")
                if not note_id:
                    return {"success": False, "error": "Missing note id"}
                async with db.execute(
                    "SELECT * FROM notes WHERE id = ? AND account_id = ?",
                    (note_id, account_id),
                ) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        return {"success": False, "error": "Note not found or unauthorized"}
                    return {
                        "success": True,
                        "note": {
                            "id": row["id"],
                            "title": row["title"],
                            "content": row["content"],
                            "tags": json.loads(row["tags"]),
                            "created_at": row["created_at"],
                            "updated_at": row["updated_at"],
                        },
                    }

            elif action == "create":
                note_id = str(uuid.uuid4())[:8]
                title = arguments.get("title", "Untitled Note")
                content = arguments.get("content", "")
                tags = json.dumps(arguments.get("tags", []))
                await db.execute(
                    "INSERT INTO notes (id, account_id, title, content, tags) VALUES (?, ?, ?, ?, ?)",
                    (note_id, account_id, title, content, tags),
                )
                await db.commit()
                return {"success": True, "note_id": note_id, "message": f"Created note '{title}'"}

            elif action == "update":
                note_id = arguments.get("id")
                title = arguments.get("title")
                content = arguments.get("content")
                if not note_id:
                    return {"success": False, "error": "Missing note id"}
                await db.execute(
                    """
                    UPDATE notes
                    SET title = COALESCE(?, title),
                        content = COALESCE(?, content),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND account_id = ?
                    """,
                    (title, content, note_id, account_id),
                )
                await db.commit()
                return {"success": True, "note_id": note_id, "message": "Note updated"}

            elif action == "delete":
                note_id = arguments.get("id")
                if not note_id:
                    return {"success": False, "error": "Missing note id"}
                await db.execute(
                    "DELETE FROM notes WHERE id = ? AND account_id = ?",
                    (note_id, account_id),
                )
                await db.commit()
                return {"success": True, "deleted_id": note_id}

            return {"success": False, "error": f"Unknown action: {action}"}
