#!/usr/bin/env python3
"""Isolated actual browser/history/local-inference demonstration. No fake provider."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=["history", "notes", "memory", "permissions", "research", "mvp", "markdown"],
        default="history",
    )
    scenario = parser.parse_args().scenario
    artifact = {
        "history": "v1-mvp-browser-history-20261108",
        "notes": "v1-mvp-browser-notes-20261108",
        "memory": "v1-mvp-browser-memory-retry-20261108",
        "permissions": "v1-mvp-browser-permissions-20261108",
        "research": "v1-browser-research-retry-20261108",
        "mvp": "v1-mvp-workflows-retry-20261108",
        "markdown": "v1-chat-markdown-20261108",
    }[scenario]
    from app.auth import provision_owner
    from app.config import settings
    from app.storage.database import get_db

    with tempfile.TemporaryDirectory(prefix="archer-browser-history-") as directory:
        runtime = Path(directory)
        runtime.chmod(0o700)
        settings.data_dir = runtime / "data"
        password = secrets.token_urlsafe(24)

        async def seed():
            await provision_owner("browser-owner", password)
            async with get_db() as db:
                owner = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
                await db.execute(
                    "INSERT INTO conversations (id,account_id,title,provider) VALUES ('synthetic-saved-planning',?,'Synthetic saved planning chat','local-ollama')",
                    (owner,),
                )
                await db.execute(
                    "INSERT INTO messages (id,conversation_id,account_id,role,content) VALUES ('synthetic-history','synthetic-saved-planning',?,'assistant','Synthetic saved history marker.')",
                    (owner,),
                )
                if scenario == "markdown":
                    await db.execute(
                        "UPDATE messages SET content=? WHERE id='synthetic-history'",
                        ("## Research summary\n\n**Useful finding** and *emphasis*.\n\n- First item\n- Second item\n\nSource: https://example.com/report\n\n[unsafe](javascript:alert(1))\n\n![remote](https://example.com/tracker.png)\n\n<script>alert(1)</script>\n\n`inline`\n\n| A | B |\n|---|---|\n| One | Two |",),
                    )
                await db.execute(
                    "INSERT INTO accounts (id,username,password_hash) VALUES ('synthetic-other','synthetic-other','test-only')"
                )
                await db.execute(
                    "INSERT INTO conversations (id,account_id,title,provider) VALUES ('synthetic-other-private','synthetic-other','Other private history','local-ollama')"
                )
                await db.execute(
                    "INSERT INTO messages (id,conversation_id,account_id,role,content) VALUES ('synthetic-other-message','synthetic-other-private','synthetic-other','assistant','OTHER_ACCOUNT_SECRET')"
                )
                if scenario == "notes":
                    await db.execute(
                        "INSERT INTO notes(id,account_id,title,content) VALUES ('synthetic-other-note','synthetic-other','Other private note','OTHER_NOTE_SECRET')"
                    )
                if scenario == "permissions":
                    await db.execute(
                        "UPDATE accounts SET password_hash=(SELECT password_hash FROM accounts WHERE id=?) WHERE id='synthetic-other'",
                        (owner,),
                    )
                    await db.execute(
                        "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'web_fetch','fetch','stale-version',1)",
                        (owner,),
                    )
                    await db.execute(
                        "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'mcp_catalog_browser','invoke','stale-mcp-version',1)",
                        (owner,),
                    )
                await db.commit()
                return owner

        owner = asyncio.run(seed())
        if scenario == "mvp":
            approved = runtime / "files" / owner
            approved.mkdir(parents=True, mode=0o700)
            (approved / "shopping.txt").write_text("Shopping list: apples, bread, and tea.\n")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        empty = runtime / "mcp.json"
        mappings = []
        if scenario == "permissions":
            for name, account in [
                ("mcp_catalog_browser", owner),
                ("mcp_other_browser", "synthetic-other"),
            ]:
                mappings.append(
                    {
                        "name": name,
                        "account_id": account,
                        "description": "Synthetic mapping metadata only; no connector execution verified."
                        if account == owner
                        else "OTHER_MAPPING_SECRET",
                        "remote_tool": "inspect",
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        "output_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        "advertised_sha256": "a" * 64,
                        "image": "sha256:" + "1" * 64,
                        "read_only": True,
                    }
                )
        empty.write_text(json.dumps(mappings))
        empty.chmod(0o600)
        environment = dict(
            os.environ,
            ARCHER_ENV="production",
            ARCHER_HOST="127.0.0.1",
            ARCHER_PORT=str(port),
            ARCHER_DATA_DIR=str(settings.data_dir),
            ARCHER_APPROVED_FILES_ROOT=str(runtime / "files"),
            ARCHER_STATIC_DIR=str(ROOT / "frontend/dist"),
            ARCHER_MCP_CONFIG_PATH=str(empty),
            ARCHER_BRAVE_SEARCH_API_KEY="",
            ARCHER_MODEL_PROVIDER="local-ollama",
            ARCHER_OLLAMA_BASE_URL="http://127.0.0.1:11434",
            ARCHER_OLLAMA_MODEL="gemma4:26b",
            ARCHER_OLLAMA_NUM_CTX="4096",
            ARCHER_RUN_TIMEOUT_SECONDS="90",
            ARCHER_EXPECTED_MODEL_DIGEST="08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68",
        )
        if scenario == "research":
            environment["ARCHER_TAVILY_API_KEY_FILE"] = str(Path.home() / ".config/archer/tavily-api-key")
        config = runtime / "browser.json"
        config.write_text(
            json.dumps(
                {
                    "base": f"http://127.0.0.1:{port}",
                    "username": "browser-owner",
                    "password": password,
                    "database": str(settings.database_path),
                    "evidence": str(ROOT / f"docs/evidence/{artifact}.json"),
                    "screenshot": str(ROOT / f"docs/evidence/{artifact}.png"),
                }
            )
        )
        config.chmod(0o600)
        with (runtime / "app.log").open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.serve"],
                cwd=ROOT / "backend",
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                with httpx.Client(trust_env=False, timeout=3) as client:
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError("Isolated app launcher exited")
                        try:
                            if client.get(f"http://127.0.0.1:{port}/api/ready").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("Isolated app not ready")
                result = subprocess.run(
                    [
                        "node",
                        str(ROOT / f"frontend/scripts/verify-{scenario}.mjs"),
                        str(config),
                    ],
                    cwd=ROOT / "frontend",
                    timeout=450 if scenario == "memory" else 150,
                    check=False,
                )
                return result.returncode
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
