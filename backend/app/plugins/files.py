"""Read-only account roots using Linux openat traversal without following symlinks."""

import os
import re
import stat
from pathlib import Path

from app.config import settings
from app.plugins.arguments import FilesArguments
from app.plugins.base import Plugin

MAX_READ_BYTES = 1024 * 1024


class ApprovedFilesPlugin(Plugin):
    name = "read_approved_file"
    description = "Read or list text files within the current account approved-files directory. Symlinks are denied."

    def __init__(self, root_dir: Path | None = None):
        self.root_dir = root_dir

    @property
    def schema(self):
        return FilesArguments.model_json_schema()

    async def execute(self, account_id, arguments):
        args = FilesArguments.model_validate(arguments)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account_id):
            return {"success": False, "error": "Access denied: invalid account root"}
        path = args.path
        parts = path.split("/") if path else []
        if path.startswith("/") or any(p in {"", "..", "."} for p in parts):
            return {"success": False, "error": "Access denied: invalid relative path"}
        descriptors = []
        try:
            root = self.root_dir or settings.approved_files_root
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(fd)
            # The account component is a directory, never a symlink or supplied tool arg.
            fd = os.open(account_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
            for i, component in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if i < len(parts) - 1 or args.action == "list":
                    flags |= os.O_DIRECTORY
                fd = os.open(component, flags, dir_fd=fd)
                descriptors.append(fd)
            info = os.fstat(fd)
            if args.action == "list":
                if not stat.S_ISDIR(info.st_mode):
                    return {"success": False, "error": "Not a directory"}
                items = []
                for name in sorted(os.listdir(fd))[:200]:
                    item = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISLNK(item.st_mode):
                        continue
                    items.append({"name": name, "is_dir": stat.S_ISDIR(item.st_mode), "size_bytes": item.st_size})
                return {"success": True, "items": items, "path": path}
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_READ_BYTES:
                return {"success": False, "error": "Regular text files up to 1 MiB only"}
            with os.fdopen(os.dup(fd), "rb") as stream:
                content = stream.read(MAX_READ_BYTES + 1)
            if len(content) > MAX_READ_BYTES:
                return {"success": False, "error": "File exceeds size limit"}
            return {"success": True, "content": content.decode("utf-8", errors="replace"), "path": path}
        except OSError:
            return {"success": False, "error": "Access denied or approved file unavailable"}
        finally:
            for fd in reversed(descriptors):
                os.close(fd)
