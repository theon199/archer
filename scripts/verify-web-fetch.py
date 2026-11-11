#!/usr/bin/env python3
"""Real public HTTPS registry and local Ollama tool selection; disposable owner."""

import asyncio
import hashlib
import json
import logging
import secrets
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
EVIDENCE = ROOT / "docs/evidence/checkpoint-22-web-fetch.json"
DIGEST = "08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68"


async def main():
    from app.auth import provision_owner
    from app.config import settings
    from app.main import app
    from app.plugins.registry import registry
    from app.runs.service import get_run, submit_run
    from app.runs.worker import Worker
    from app.storage.database import get_db

    EVIDENCE.write_text(json.dumps({"passed": False, "state": "in_progress"}) + "\n")
    checks = {}
    observations = {}
    with tempfile.TemporaryDirectory(prefix="archer-web-fetch-") as directory:
        settings.data_dir = Path(directory)
        settings.model_provider = "local-ollama"
        settings.ollama_base_url = "http://127.0.0.1:11434"
        settings.ollama_model = "gemma4:26b"
        settings.ollama_num_ctx = 4096
        settings.expected_model_digest = DIGEST
        settings.run_timeout_seconds = 90
        settings.host = "127.0.0.1"
        password = secrets.token_urlsafe(24)
        await provision_owner("web-fetch-drill", password)
        async with get_db() as db:
            account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
        url = "https://www.sqlite.org/wal.html"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post("/api/auth/login", json={"username": "web-fetch-drill", "password": password})
            login.raise_for_status()
            client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
            denied = await registry.dispatch("web_fetch", account, {"url": url}, operation_id="deny-public")
            checks["default_denied"] = denied["status"] == "denied"
            grant = await client.put("/api/permissions/web_fetch/fetch", json={"enabled": True})
            checks["owner_grant"] = grant.status_code == 200
            fetched = await registry.dispatch("web_fetch", account, {"url": url}, operation_id="real-public-fetch")
            checks["real_public_document"] = (
                fetched["success"] and fetched["title"] == "Write-Ahead Logging" and "Write-Ahead" in fetched["text"]
            )
            checks["bounded_untrusted_text"] = (
                len(fetched.get("text", "")) <= 16000 and fetched.get("trust") == "untrusted_web_content"
            )
            repeated = await registry.dispatch("web_fetch", account, {"url": url}, operation_id="real-public-fetch")
            checks["same_operation_returns_durable_result"] = fetched == repeated
            blocked = await registry.dispatch(
                "web_fetch", account, {"url": "https://localhost.localdomain/"}, operation_id="deny-local-dns"
            )
            checks["actual_local_dns_not_retrieved"] = not blocked["success"] and blocked["status"] == "failed"
            async with get_db() as db:
                rows = await (
                    await db.execute(
                        "SELECT arguments,outcome FROM journal_entries WHERE tool_name='web_fetch' AND permission_decision='allowed'"
                    )
                ).fetchall()
                checks["one_public_success_journal"] = (
                    sum(json.loads(row["outcome"])["status"] == "succeeded" for row in rows) == 1
                )
                checks["journal_does_not_copy_url_or_body"] = all(
                    url not in row["arguments"] and "Write-Ahead" not in row["outcome"] for row in rows
                )
            async with httpx.AsyncClient(trust_env=False, timeout=5) as metadata:
                tags = (await metadata.get(settings.ollama_base_url + "/api/tags")).json()
            checks["real_local_model_digest"] = any(
                item["name"] == settings.ollama_model and item["digest"] == DIGEST for item in tags["models"]
            )
            if not checks["real_local_model_digest"]:
                raise RuntimeError("Local model artifact mismatch")
            run = await submit_run(
                account,
                "Use web_fetch exactly once to read https://www.sqlite.org/robots.txt . After the tool returns successfully, answer briefly with the first User-agent directive from that document. Do not use web_search or save a note.",
                None,
                "local-ollama",
                "real-model-web-fetch",
            )
            with Worker() as worker:
                await worker.run_one()
            final = await get_run(account, run["id"])
            async with get_db() as db:
                calls = await (
                    await db.execute("SELECT data FROM run_events WHERE run_id=? AND event='tool_call'", (run["id"],))
                ).fetchall()
                outcomes = await (
                    await db.execute("SELECT data FROM run_events WHERE run_id=? AND event='tool_result'", (run["id"],))
                ).fetchall()
                journal = await (
                    await db.execute(
                        "SELECT outcome FROM journal_entries WHERE run_id=? AND tool_name='web_fetch'", (run["id"],)
                    )
                ).fetchall()
                operation = await (
                    await db.execute(
                        "SELECT status FROM tool_operations WHERE run_id=? AND tool_name='web_fetch'", (run["id"],)
                    )
                ).fetchall()
            checks["real_model_selected_web_fetch"] = any(json.loads(row[0])["tool"] == "web_fetch" for row in calls)
            checks["real_model_fetch_success"] = any(
                json.loads(row[0])["tool"] == "web_fetch" and json.loads(row[0])["result"]["success"]
                for row in outcomes
            )
            checks["real_model_run_succeeded"] = final["status"] == "succeeded" and bool(final["output"].strip())
            checks["model_output_grounded_in_document"] = "user-agent" in final["output"].lower()
            checks["model_action_durable_and_journal_accurate"] = (
                bool(journal)
                and all(json.loads(row[0])["status"] == "succeeded" for row in journal)
                and all(row[0] == "succeeded" for row in operation)
            )
            checks["revoke"] = (
                await client.put("/api/permissions/web_fetch/fetch", json={"enabled": False})
            ).status_code == 200
            checks["revoked_grant_denies_cached_operation"] = (
                await registry.dispatch("web_fetch", account, {"url": url}, operation_id="real-public-fetch")
            )["status"] == "denied"
            observations = {
                "public_document_bytes": fetched.get("bytes_read"),
                "public_text_characters": len(fetched.get("text", "")),
                "model_run_status": final["status"],
                "model_output_sha256": hashlib.sha256(final["output"].encode()).hexdigest(),
                "model_tool_call_count": len(calls),
            }
    result = {
        "passed": all(checks.values()),
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "observations": observations,
        "model": "gemma4:26b",
        "model_digest": DIGEST,
        "scope": "Real public HTTPS with production DNS-pinned verified TLS transport; actual local Ollama selection and response, real worker/SQLite/registry/auth/journal, synthetic disposable owner. No mocked inference, Brave search, third-party MCP or browser claim.",
    }
    EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        code = asyncio.run(asyncio.wait_for(main(), timeout=150))
    except Exception as error:  # noqa: BLE001 - Preserve sanitized failed-drill evidence.
        result = {
            "passed": False,
            "error_type": type(error).__name__,
            "checked_at": datetime.now(UTC).isoformat(),
            "scope": "Real retrieval/model drill failed; no fake integration substituted",
        }
        EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        code = 1
    raise SystemExit(code)
