#!/usr/bin/env python3
"""Actual offline CLI transfer/crypto restore on disposable synthetic data only."""

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
EVIDENCE = ROOT / "docs/evidence/checkpoint-19-legacy-apply.json"


def main():
    from httpx import ASGITransport, AsyncClient

    from app.auth import provision_owner, token_digest
    from app.backup import keygen, restore
    from app.config import settings
    from app.main import app
    from app.storage.database import get_db

    EVIDENCE.write_text(json.dumps({"passed": False, "state": "in_progress"}) + "\n")
    checks = {}
    with tempfile.TemporaryDirectory(prefix="archer-legacy-apply-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        settings.data_dir = root / "runtime"
        settings.approved_files_root = root / "approved"
        password = secrets.token_urlsafe(24)
        legacy_token = secrets.token_urlsafe(24)

        async def seed():
            await provision_owner("synthetic-transfer-owner", password)
            async with get_db() as db:
                owner = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
                await db.execute(
                    "INSERT INTO accounts(id,username,password_hash) VALUES ('synthetic-legacy','synthetic-legacy','local_dev_bypass')"
                )
                await db.execute(
                    "INSERT INTO notes(id,account_id,title,content) VALUES ('synthetic-import-note','synthetic-legacy','Synthetic legacy note','SYNTHETIC_LEGACY_CONTENT')"
                )
                await db.execute(
                    "INSERT INTO note_revisions(note_id,account_id,revision,title,content,tags) SELECT id,account_id,revision,title,content,tags FROM notes"
                )
                await db.execute(
                    "INSERT INTO notes(id,account_id,title,content) VALUES ('synthetic-owner-note',?,'Owner note','OWNER_CONTENT')",
                    (owner,),
                )
                await db.execute(
                    "INSERT INTO sessions(id,account_id,token,expires_at) VALUES ('legacy-session','synthetic-legacy',?,9999999999)",
                    (token_digest(legacy_token),),
                )
                await db.execute(
                    "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'personal_notes','get','1',1)",
                    (owner,),
                )
                await db.execute(
                    "INSERT INTO memories(id,account_id,memory_type,content,evidence,subject_key,value) VALUES ('synthetic-legacy-memory','synthetic-legacy','preference','Unverified legacy content','old-source','response_length','concise')"
                )
                await db.commit()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                login = await client.post(
                    "/api/auth/login", json={"username": "synthetic-transfer-owner", "password": password}
                )
                return owner, login.json()["access_token"]

        owner, old_owner_token = asyncio.run(seed())
        key = root / "key"
        keygen(key)
        env = dict(
            os.environ,
            ARCHER_DATA_DIR=str(settings.data_dir),
            ARCHER_APPROVED_FILES_ROOT=str(settings.approved_files_root),
            ARCHER_ENV="production",
        )
        common = ["--source-account", "synthetic-legacy", "--target-owner", owner]

        def command(arguments):
            return subprocess.run(
                [sys.executable, "-m", "app.legacy_migration", *arguments],
                cwd=ROOT / "backend",
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

        planned = command(["plan", *common])
        if planned.returncode != 0:
            raise RuntimeError("Synthetic CLI plan failed")
        plan = json.loads(planned.stdout)
        applied = command(
            [
                "apply",
                *common,
                "--expected-fingerprint",
                plan["fingerprint_sha256"],
                "--policy-version",
                plan["policy_version"],
                "--backup-output",
                str(root / "before.arbk"),
                "--backup-key",
                str(key),
                "--offline",
            ]
        )
        checks["actual_cli_apply_succeeded"] = applied.returncode == 0
        if applied.returncode != 0:
            raise RuntimeError("Synthetic CLI transfer failed")
        output = json.loads(applied.stdout)
        checks["cli_output_contains_no_content_or_credentials"] = all(
            value not in applied.stdout
            for value in [password, legacy_token, old_owner_token, "SYNTHETIC_LEGACY_CONTENT"]
        )
        with sqlite3.connect(settings.database_path) as db:
            checks["imported_note_owner_and_revision_preserved"] = db.execute(
                "SELECT account_id,revision FROM notes WHERE id='synthetic-import-note'"
            ).fetchone() == (owner, 1)
            checks["owner_content_preserved"] = (
                db.execute("SELECT content FROM notes WHERE id='synthetic-owner-note'").fetchone()[0] == "OWNER_CONTENT"
            )
            checks["foreign_keys_valid"] = db.execute("PRAGMA foreign_key_check").fetchone() is None
            checks["sessions_revoked"] = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
            checks["active_grants_disabled"] = (
                db.execute("SELECT COUNT(*) FROM tool_grants WHERE enabled=1").fetchone()[0] == 0
            )
            checks["legacy_memory_quarantined"] = db.execute(
                "SELECT subject_key,value,origin FROM memories WHERE id='synthetic-legacy-memory'"
            ).fetchone() == (None, None, "legacy")
            checks["single_provenance_journal"] = (
                db.execute(
                    "SELECT COUNT(*) FROM journal_entries WHERE tool_name='legacy_migration' AND account_id=?", (owner,)
                ).fetchone()[0]
                == 1
            )

        async def authentication():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                for name, token in [("legacy", legacy_token), ("owner", old_owner_token)]:
                    checks[name + "_old_session_denied"] = (
                        await client.get("/api/v1/conversations", headers={"Authorization": "Bearer " + token})
                    ).status_code == 401
                signed = await client.post(
                    "/api/auth/login", json={"username": "synthetic-transfer-owner", "password": password}
                )
                checks["existing_owner_password_unchanged"] = signed.status_code == 200
                headers = {"Authorization": "Bearer " + signed.json()["access_token"]}
                checks["imported_note_denied_until_explicit_grant"] = (
                    await client.get("/api/notes/synthetic-import-note", headers=headers)
                ).status_code == 403
                granted = await client.put(
                    "/api/permissions/personal_notes/get", headers=headers, json={"enabled": True}
                )
                note = await client.get("/api/notes/synthetic-import-note", headers=headers)
                checks["owner_can_review_import_after_explicit_grant"] = (
                    granted.status_code == 200 and note.json()["note"]["content"] == "SYNTHETIC_LEGACY_CONTENT"
                )

        asyncio.run(authentication())
        restore_result = restore(root / "before.arbk", root / "restored", key)
        with sqlite3.connect(root / "restored" / "archer.db") as db:
            checks["actual_encrypted_backup_restores_original_ownership"] = (
                restore_result["restored"]
                and db.execute("SELECT account_id FROM notes WHERE id='synthetic-import-note'").fetchone()[0]
                == "synthetic-legacy"
            )
            checks["restored_owner_data_preserved"] = (
                db.execute("SELECT content FROM notes WHERE id='synthetic-owner-note'").fetchone()[0] == "OWNER_CONTENT"
            )
            checks["restored_sessions_quarantined"] = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        result = {
            "passed": all(checks.values()),
            "checks": checks,
            "checked_at": datetime.now(UTC).isoformat(),
            "policy_version": output["policy_version"],
            "scope": "Actual local plan/apply CLI subprocess, encrypted backup and authenticated restore, SQLite composite ownership and in-process ASGI auth/tools with synthetic disposable data. No production ownership transfer or model integration claimed.",
        }
        EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return 0 if result["passed"] else 1


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        status = main()
    except Exception as error:
        failure = {
            "passed": False,
            "error_type": type(error).__name__,
            "checked_at": datetime.now(UTC).isoformat(),
            "scope": "Synthetic migration verification failed; no production data used",
        }
        EVIDENCE.write_text(json.dumps(failure, indent=2) + "\n")
        print(json.dumps(failure))
        status = 1
    raise SystemExit(status)
