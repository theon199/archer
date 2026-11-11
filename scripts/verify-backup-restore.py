#!/usr/bin/env python3
"""Repeatable local synthetic encrypted-backup drill; no model/provider calls.

Run from backend/: uv run --locked --extra dev python ../scripts/verify-backup-restore.py
"""

import argparse
import asyncio
import importlib.util
import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import cryptography
import pytest
from httpx import ASGITransport, AsyncClient

# Use the same synthetic seed as the restore regression tests, without pytest
# fixtures or mocks of encryption, SQLite, subprocesses, API, files or providers.
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
spec = importlib.util.spec_from_file_location("archer_backup_cases", REPO / "backend/tests/test_backup.py")
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


async def main(evidence_path):
    from app.config import settings
    from app.main import app
    from app.runs.worker import Worker

    evidence = {
        "kind": "actual_encrypted_backup_cli_restore_drill",
        "started_at": datetime.now(UTC).isoformat(),
        "synthetic_data_only": True,
        "external_copy_verified": False,
        "platform": platform.machine(),
        "sqlite_version": sqlite3.sqlite_version,
        "cryptography_version": cryptography.__version__,
    }
    checks = {}
    with tempfile.TemporaryDirectory(prefix="archer-backup-drill-") as directory, pytest.MonkeyPatch.context() as patch:
        root = Path(directory)
        fixture = await cases.seed(root, patch)
        key = root / "cli.key"

        def cli(*arguments):
            result = subprocess.run(
                [sys.executable, "-m", "app.backup", *map(str, arguments)],
                cwd=REPO / "backend",
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode:
                raise RuntimeError("Backup CLI drill command failed")
            return json.loads(result.stdout)

        evidence["keygen"] = await asyncio.to_thread(cli, "keygen", "--key-file", key)
        observer = sqlite3.connect(settings.database_path)
        observer.execute("PRAGMA journal_mode=WAL")
        try:
            updated = await cases.registry.dispatch(
                "personal_notes",
                fixture["account"],
                {
                    "action": "update",
                    "id": fixture["note"]["note_id"],
                    "expected_revision": 1,
                    "content": "committed WAL body",
                },
                operation_id="wal-update",
            )
            checks["wal_revision_committed"] = (
                updated["revision"] == 2 and Path(str(settings.database_path) + "-wal").exists()
            )
            evidence["backup"] = await asyncio.to_thread(
                cli,
                "create",
                "--data-dir",
                fixture["source"],
                "--files-root",
                settings.approved_files_root,
                "--output",
                fixture["output"],
                "--key-file",
                key,
            )
        finally:
            observer.close()
        destination = root / "restored"
        evidence["restore"] = await asyncio.to_thread(
            cli, "restore", "--input", fixture["output"], "--destination", destination, "--key-file", key
        )
        checks["ciphertext_hides_fixture_body"] = b"committed WAL body" not in fixture["output"].read_bytes()
        checks["file_bytes_preserved"] = (
            destination / "approved_files" / fixture["account"] / "approved.txt"
        ).read_text() == "Synthetic approved file"
        with sqlite3.connect(destination / "archer.db") as db:
            checks["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            checks["foreign_keys"] = db.execute("PRAGMA foreign_key_check").fetchone() is None
            checks["note_revision_and_content"] = db.execute("SELECT content,revision FROM notes").fetchone() == (
                "committed WAL body",
                2,
            )
            checks["note_history"] = db.execute("SELECT COUNT(*) FROM note_revisions").fetchone()[0] == 2
            checks["grants_preserved"] = db.execute("SELECT COUNT(*) FROM tool_grants").fetchone()[0] == 4
            checks["grants_disabled_until_review"] = (
                db.execute("SELECT COUNT(*) FROM tool_grants WHERE enabled=1").fetchone()[0] == 0
            )
            checks["sessions_revoked"] = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
            checks["deleted_preference_stays_deleted"] = (
                db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
                and db.execute("SELECT subject_key FROM memory_suppressions").fetchone()[0] == "response_length"
            )
            checks["learning_disabled"] = db.execute(
                "SELECT learning_enabled,retrieval_enabled FROM memory_settings"
            ).fetchone() == (0, 0)
            checks["unfinished_runs_quarantined"] = {r[0] for r in db.execute("SELECT status FROM runs")} == {
                "needs_reconciliation"
            }
            checks["uncertain_tool_quarantined"] = (
                db.execute("SELECT status FROM tool_operations WHERE operation_id='unfinished-note'").fetchone()[0]
                == "needs_reconciliation"
            )
        patch.setattr(settings, "data_dir", destination)
        patch.setattr(settings, "approved_files_root", destination / "approved_files")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            checks["old_session_rejected"] = (
                await client.get("/api/notes", headers={"Authorization": f"Bearer {fixture['token']}"})
            ).status_code == 401
            login = await client.post("/api/auth/login", json={"username": "drill-owner", "password": cases.PASSWORD})
            checks["fresh_owner_login"] = login.status_code == 200
            denied = await client.get("/api/notes", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
            checks["review_required_before_note_access"] = denied.status_code == 403
            renewed = await client.put(
                "/api/permissions/personal_notes/list",
                headers={"Authorization": f"Bearer {login.json()['access_token']}"},
                json={"enabled": True},
            )
            checks["owner_can_review_and_renew_grant"] = renewed.status_code == 200
            notes = await client.get("/api/notes", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
            checks["restored_note_visible"] = notes.status_code == 200 and notes.json()[0]["revision"] == 2
        with Worker() as worker:
            checks["no_automatic_job_replay"] = not await worker.run_one()
    evidence.update(checks=checks, passed=all(checks.values()), finished_at=datetime.now(UTC).isoformat())
    if evidence_path:
        evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    if not evidence["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    asyncio.run(main(parser.parse_args().evidence))
