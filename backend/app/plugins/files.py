"""Approved Files Plugin - Safe, sandboxed file reading."""

from pathlib import Path
from typing import Any

from app.config import settings
from app.plugins.base import Plugin

MAX_READ_BYTES = 1024 * 1024 * 5  # 5 MB limit


class ApprovedFilesPlugin(Plugin):
    """Safely browse and read files within an approved workspace root."""

    def __init__(self, root_dir: Path | None = None):
        self.root = (root_dir or settings.approved_files_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "read_approved_file"

    @property
    def description(self) -> str:
        return "List, search, or read text content from permitted files inside the approved storage root."

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "read"],
                    "description": "Whether to list directory contents or read a file.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative path inside the approved files directory.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, account_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments.get("action", "list")
        rel_path = arguments.get("path", "").strip().lstrip("/")

        target = (self.root / rel_path).resolve()

        # Strict security check: path must stay within root
        if not str(target).startswith(str(self.root)):
            return {
                "success": False,
                "error": "Access denied: Path escapes approved files boundary.",
            }

        if action == "list":
            if not target.exists():
                return {"success": False, "error": f"Directory not found: {rel_path}"}
            if not target.is_dir():
                return {"success": False, "error": f"Path is not a directory: {rel_path}"}

            items = []
            for item in target.iterdir():
                items.append(
                    {
                        "name": item.name,
                        "is_dir": item.is_dir(),
                        "size_bytes": item.stat().st_size if item.is_file() else 0,
                    }
                )
            return {"success": True, "path": rel_path or "/", "items": items}

        elif action == "read":
            if not target.exists():
                return {"success": False, "error": f"File not found: {rel_path}"}
            if not target.is_file():
                return {"success": False, "error": f"Path is not a regular file: {rel_path}"}

            if target.stat().st_size > MAX_READ_BYTES:
                return {"success": False, "error": "File exceeds maximum permitted size of 5 MB."}

            try:
                content = target.read_text(encoding="utf-8", errors="replace")
                return {"success": True, "path": rel_path, "content": content}
            except Exception as e:
                return {"success": False, "error": f"Failed to read file: {e}"}

        return {"success": False, "error": f"Unsupported action: {action}"}
