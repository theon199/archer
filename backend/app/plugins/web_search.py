"""Permission-controlled web search using Tavily's basic search."""

import json
import logging
import os
import stat
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.plugins.arguments import SearchArguments
from app.plugins.base import Plugin

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 1024 * 1024


def tavily_key():
    """Read only an owner-only regular credential file; never log its contents."""
    path = settings.tavily_api_key_file
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None
            data = stream.read(513)
        if not 1 <= len(data) <= 512 or any(byte < 33 or byte > 126 for byte in data):
            return None
        return data.decode("ascii")
    except OSError:
        return None


class WebSearchPlugin(Plugin):
    """Search the web with source links using Tavily."""

    version = "2-tavily"

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "Search Tavily for sources and excerpts; the search query is sent to Tavily."

    @property
    def schema(self) -> dict[str, Any]:
        return SearchArguments.model_json_schema()

    async def execute(self, account_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = SearchArguments.model_validate(arguments).model_dump()
        query = arguments.get("query", "").strip()
        count = min(arguments.get("count", 3), 5)

        if not query:
            return {"success": False, "error": "Query cannot be empty."}

        key = tavily_key()
        if key:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
            }
            url = "https://api.tavily.com/search"
            payload = {
                "query": query,
                "search_depth": "basic",
                "max_results": count,
                "include_answer": False,
                "include_raw_content": False,
                "auto_parameters": False,
            }
            try:
                async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                    async with client.stream("POST", url, headers=headers, json=payload) as resp:
                        if resp.status_code != 200:
                            return {"success": False, "error": f"Tavily API returned status {resp.status_code}"}
                        body = bytearray()
                        async for chunk in resp.aiter_bytes():
                            if len(chunk) > MAX_RESPONSE_BYTES - len(body):
                                return {"success": False, "error": "Search response exceeded size limit"}
                            body.extend(chunk)
                        data = json.loads(body)
                        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                            return {"success": False, "error": "Search response was invalid"}
                        results = []
                        for item in data.get("results", [])[:count]:
                            if not isinstance(item, dict) or not all(
                                isinstance(item.get(field), str) for field in ("title", "url", "content")
                            ):
                                return {"success": False, "error": "Search response was invalid"}
                            address = item["url"]
                            parsed = urlsplit(address)
                            if (
                                len(address) > 2048
                                or parsed.scheme not in {"http", "https"}
                                or not parsed.hostname
                                or parsed.username is not None
                                or parsed.password is not None
                            ):
                                return {"success": False, "error": "Search response was invalid"}
                            results.append(
                                {
                                    "title": item["title"][:500],
                                    "url": address,
                                    "snippet": item["content"][:4000],
                                }
                            )
                        return {"success": True, "query": query, "results": results, "untrusted_content": True}
            except Exception:
                return {"success": False, "error": "Search connection failed"}

        return {
            "success": False,
            "status": "unavailable",
            "error": "Tavily API key is not configured or is not private.",
        }
