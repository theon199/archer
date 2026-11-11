#!/usr/bin/env python3
"""Actual owned-process SIGKILL and real worker restart, synthetic note proposal."""

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
EVIDENCE = ROOT / "docs/evidence/checkpoint-20-note-crash.json"


def main():
    from app.auth import provision_owner
    from app.config import settings

    checks = {}
    observations = {}
    EVIDENCE.write_text(json.dumps({"passed": False, "state": "in_progress"}) + "\n")
    for phase in ["before_commit", "after_commit"]:
        expected = 0 if phase == "before_commit" else 1
        with tempfile.TemporaryDirectory(prefix="archer-note-crash-") as directory:
            runtime = Path(directory)
            runtime.chmod(0o700)
            settings.data_dir = runtime / "data"
            asyncio.run(provision_owner("synthetic-crash-owner", "synthetic-only-crash-password"))
            database = settings.database_path
            with closing(sqlite3.connect(database)) as db, db:
                account = db.execute("SELECT id FROM accounts WHERE is_owner=1").fetchone()[0]
                db.execute(
                    "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'personal_notes','create','1',1)",
                    (account,),
                )
            empty = runtime / "mcp.json"
            empty.write_text("[]")
            empty.chmod(0o600)
            environment = dict(
                os.environ,
                ARCHER_ENV="production",
                ARCHER_HOST="127.0.0.1",
                ARCHER_DATA_DIR=str(settings.data_dir),
                ARCHER_APPROVED_FILES_ROOT=str(runtime / "approved"),
                ARCHER_MCP_CONFIG_PATH=str(empty),
                ARCHER_STATIC_DIR=str(ROOT / "frontend/dist"),
                ARCHER_MODEL_PROVIDER="local-ollama",
                ARCHER_OLLAMA_BASE_URL="http://127.0.0.1:11434",
                ARCHER_OLLAMA_NUM_CTX="4096",
            )
            marker = runtime / "ready.json"
            restarted = None
            with (runtime / "driver.log").open("w") as log:
                driver = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "tests.fixtures.note_crash_driver",
                        "--phase",
                        phase,
                        "--marker",
                        str(marker),
                    ],
                    cwd=ROOT / "backend",
                    env=environment,
                    stdout=log,
                    stderr=log,
                )
                try:
                    deadline = time.monotonic() + 20
                    while not marker.is_file() and time.monotonic() < deadline:
                        if driver.poll() is not None:
                            raise RuntimeError("Owned crash fixture exited before boundary")
                        time.sleep(0.05)
                    if not marker.is_file():
                        raise RuntimeError("Owned crash fixture boundary deadline")
                    identity = json.loads(marker.read_text())
                    with closing(sqlite3.connect(database)) as db, db:
                        visible = db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                        checks[phase + "_boundary_visibility"] = visible == expected
                    os.kill(driver.pid, signal.SIGKILL)
                    driver.wait(timeout=5)
                    checks[phase + "_actual_sigkill"] = driver.returncode == -signal.SIGKILL
                    restarted = subprocess.Popen(
                        [sys.executable, "-m", "app.runs.worker"],
                        cwd=ROOT / "backend",
                        env=environment,
                        stdout=log,
                        stderr=log,
                    )
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if restarted.poll() is not None:
                            raise RuntimeError("Actual restarted worker exited")
                        with closing(sqlite3.connect(database)) as db, db:
                            status = db.execute("SELECT status FROM runs WHERE id=?", (identity["run_id"],)).fetchone()[
                                0
                            ]
                            recovered = db.execute(
                                "SELECT COUNT(*) FROM journal_entries WHERE tool_name='run_recovery'"
                            ).fetchone()[0]
                            heartbeat = db.execute("SELECT COUNT(*) FROM worker_status").fetchone()[0]
                        if status == "needs_reconciliation" and recovered == 1 and heartbeat == 1:
                            break
                        time.sleep(0.05)
                    else:
                        raise RuntimeError("Actual worker recovery deadline")
                    time.sleep(0.5)  # Several real worker claim cycles; never a provider retry.
                    with closing(sqlite3.connect(database)) as db, db:
                        checks[phase + "_integrity"] = (
                            db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                            and db.execute("PRAGMA foreign_key_check").fetchone() is None
                        )
                        checks[phase + "_run_uncertainty_retained"] = (
                            db.execute("SELECT status FROM runs").fetchone()[0] == "needs_reconciliation"
                        )
                        checks[phase + "_note_atomicity"] = (
                            db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == expected
                        )
                        checks[phase + "_operation_atomicity"] = (
                            db.execute("SELECT COUNT(*) FROM tool_operations").fetchone()[0] == expected
                        )
                        checks[phase + "_revision_atomicity"] = (
                            db.execute("SELECT COUNT(*) FROM note_revisions").fetchone()[0] == expected
                        )
                        checks[phase + "_single_run_no_replay"] = (
                            db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
                            and db.execute("SELECT generation FROM runs").fetchone()[0] == 1
                        )
                        checks[phase + "_no_assistant_success_message"] = (
                            db.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 0
                        )
                        audit = db.execute(
                            "SELECT outcome FROM journal_entries WHERE tool_name='run_recovery'"
                        ).fetchall()
                        checks[phase + "_durable_uncertainty_journal"] = (
                            len(audit) == 1
                            and json.loads(audit[0][0])["status"] == "needs_reconciliation"
                            and not json.loads(audit[0][0])["success"]
                        )
                        tool_audit = db.execute(
                            "SELECT outcome FROM journal_entries WHERE tool_name='personal_notes'"
                        ).fetchall()
                        checks[phase + "_accurate_tool_journal"] = len(tool_audit) == expected and (
                            not expected or json.loads(tool_audit[0][0])["success"] is True
                        )
                        if expected:
                            checks[phase + "_confirmed_operation_not_rewritten"] = (
                                db.execute("SELECT status FROM tool_operations").fetchone()[0] == "succeeded"
                            )
                        events = db.execute("SELECT data FROM run_events WHERE event='done'").fetchall()
                        checks[phase + "_terminal_event_durable"] = (
                            len(events) == 1 and json.loads(events[0][0])["status"] == "needs_reconciliation"
                        )
                        observations[phase] = {
                            "visible_notes_before_kill": visible,
                            "notes_after_restart": db.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
                            "terminal_status": status,
                        }
                finally:
                    for child in [driver, restarted]:
                        if child is not None and child.poll() is None:
                            child.terminate()
                            try:
                                child.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait(timeout=5)
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "observations": observations,
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Actual SIGKILL of exact owned fixture at real SQLite note transaction boundary and restart of real app.runs.worker. Synthetic proposal/notes, no model selection or fake inference provider. Process crash on this host, not power-loss/storage-failure proof.",
    }
    EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    EVIDENCE = parser.parse_args().evidence
    try:
        status = main()
    except Exception as error:  # noqa: BLE001 - Persist sanitized failure evidence.
        failure = {
            "passed": False,
            "error_type": type(error).__name__,
            "checked_at": datetime.now(UTC).isoformat(),
            "scope": "Synthetic note crash drill failed; no integration success inferred",
        }
        EVIDENCE.write_text(json.dumps(failure, indent=2) + "\n")
        print(json.dumps(failure))
        status = 1
    raise SystemExit(status)
