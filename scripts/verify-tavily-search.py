#!/usr/bin/env python3
"""One real Tavily search through Archer's registry, with disposable data.

Requires ARCHER_TAVILY_API_KEY_FILE to point to a private key file. Sends one fixed
public query and may incur provider charges. Never writes the credential.
"""

import argparse
import asyncio
import json
import logging
import secrets
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


async def main():
    from app.auth import provision_owner
    from app.config import settings
    from app.main import app
    from app.plugins.registry import registry
    from app.plugins.web_search import tavily_key
    from app.storage.database import get_db

    key = tavily_key()
    if not key:
        raise ValueError("Tavily key is unavailable")
    checks = {}
    query = "SQLite write ahead logging documentation"
    with tempfile.TemporaryDirectory(prefix="archer-tavily-search-") as directory:
        settings.data_dir = Path(directory)
        settings.tools_default_enabled = False  # Exercise the explicit grant/revoke path in isolation.
        password = secrets.token_urlsafe(24)
        await provision_owner("tavily-search-drill", password)
        async with get_db() as db:
            account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post("/api/auth/login", json={"username": "tavily-search-drill", "password": password})
            login.raise_for_status()
            client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
            args = {"query": query, "count": 3}
            denied = await registry.dispatch("web_search", account, args, operation_id="search-denied")
            checks["default_denied"] = denied["status"] == "denied"
            checks["owner_grant"] = (
                await client.put("/api/permissions/web_search/search", json={"enabled": True})
            ).status_code == 200
            found = await registry.dispatch("web_search", account, args, operation_id="search-once")
            results = found.get("results", [])
            checks["real_search_succeeded"] = found.get("success") is True
            checks["usable_citations"] = bool(results) and all(
                isinstance(item.get("title"), str)
                and bool(item["title"].strip())
                and isinstance(item.get("url"), str)
                and urlsplit(item["url"]).scheme in {"https", "http"}
                and bool(urlsplit(item["url"]).hostname)
                for item in results
            )
            checks["replay_same_result"] = (
                await registry.dispatch("web_search", account, args, operation_id="search-once") == found
            )
            async with get_db() as db:
                rows = await (
                    await db.execute(
                        "SELECT arguments,outcome FROM journal_entries WHERE tool_name='web_search' AND permission_decision='allowed'"
                    )
                ).fetchall()
            checks["single_redacted_success_journal"] = len(rows) == 1 and (
                json.loads(rows[0]["outcome"]).get("status") == "succeeded"
                and query not in rows[0]["arguments"]
                and key not in str(dict(rows[0]))
            )
            checks["revoke"] = (
                await client.put("/api/permissions/web_search/search", json={"enabled": False})
            ).status_code == 200
            checks["revoked_cache_denied"] = (
                await registry.dispatch("web_search", account, args, operation_id="search-once")
            )["status"] == "denied"
    return {"passed": all(checks.values()), "checks": checks, "result_count": len(results)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path, help="New file for the sanitized result")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        with args.evidence.open("x") as evidence:
            try:
                result = asyncio.run(asyncio.wait_for(main(), 45))
            except Exception as error:  # noqa: BLE001 - Never persist provider bodies or credentials.
                result = {"passed": False, "error_type": type(error).__name__}
            result["checked_at"] = datetime.now(UTC).isoformat()
            result["scope"] = (
                "One real Tavily query through disposable owner, registry, permissions and journal; no model or browser demonstration."
            )
            json.dump(result, evidence, indent=2)
            evidence.write("\n")
        print(json.dumps(result))
        raise SystemExit(0 if result["passed"] else 1)
    except FileExistsError:
        print("Evidence path already exists", file=sys.stderr)
        raise SystemExit(2) from None
