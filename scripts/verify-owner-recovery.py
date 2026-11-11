#!/usr/bin/env python3
"""Real Linux PTY/CLI/auth drill; synthetic private data, no transcript logging."""

import asyncio
import errno
import hashlib
import json
import os
import pty
import secrets
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import termios
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence/checkpoint-13-owner-recovery-pty.json"
sys.path.insert(0, str(ROOT / "backend"))


def terminal(account, environment, responses):
    child, descriptor = pty.fork()
    if child == 0:
        os.chdir(ROOT / "backend")
        os.execve(sys.executable, [sys.executable, "-m", "app.owner_recovery", "--account-id", account], environment)
    transcript, step, status, echo_disabled = bytearray(), 0, None, True
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.1)
            if ready:
                try:
                    data = os.read(descriptor, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    data = b""
                if data:
                    transcript.extend(data)
                elif status is not None:
                    break
            if step < len(responses) and responses[step][0] in transcript:
                echo_disabled &= not bool(termios.tcgetattr(descriptor)[3] & termios.ECHO)
                os.write(descriptor, responses[step][1])
                step += 1
            if status is None:
                finished, value = os.waitpid(child, os.WNOHANG)
                if finished:
                    status = value
            if status is not None and not ready:
                break
        if status is None or step != len(responses):
            raise RuntimeError("PTY child deadline or prompt mismatch; transcript not logged")
        return os.waitstatus_to_exitcode(status), bytes(transcript), echo_disabled
    finally:
        if status is None:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(descriptor)


def main():
    EVIDENCE.write_text(
        json.dumps({"passed": False, "state": "in_progress", "checked_at": datetime.now(UTC).isoformat()}) + "\n"
    )
    from httpx import ASGITransport, AsyncClient

    from app.auth import provision_owner
    from app.config import settings
    from app.main import app
    from app.runs.worker import Worker
    from app.storage.database import get_db

    checks = {}
    password, replacement = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory(prefix="archer-recovery-pty-") as directory:
        runtime = Path(directory)
        runtime.chmod(0o700)
        settings.data_dir = runtime
        environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
        environment.update(ARCHER_DATA_DIR=str(runtime), ARCHER_ENV="production")

        async def login(value):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/api/auth/login", json={"username": "synthetic-pty-owner", "password": value})

        async def seed():
            await provision_owner("synthetic-pty-owner", password)
            async with get_db() as db:
                account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
                await db.execute(
                    "INSERT INTO notes(id,account_id,title,content) VALUES ('synthetic-note',?,'Synthetic','Keep this content')",
                    (account,),
                )
                await db.execute(
                    "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'personal_notes','list','1',1)",
                    (account,),
                )
                await db.commit()
            response = await login(password)
            if response.status_code != 200:
                raise RuntimeError("Synthetic owner login failed")
            return account, response.json()["access_token"]

        account, old_token = asyncio.run(seed())

        def state():
            with sqlite3.connect(settings.database_path) as db:
                return hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()

        baseline = state()
        first, second = b"New owner password (at least 12 characters): ", b"Confirm new password: "
        success_inputs = [(first, replacement.encode() + b"\n"), (second, replacement.encode() + b"\n")]

        def run_case(name, owner, values, expected):
            code, transcript, echo = terminal(owner, environment, values)
            checks[name] = code != 0 and expected in transcript and state() == baseline
            checks[name + "_echo_disabled"] = echo
            checks[name + "_no_secret_output"] = all(
                value.encode() not in transcript for value in [password, replacement]
            )
            checks[name + "_no_traceback"] = b"Traceback" not in transcript

        run_case(
            "confirmation_mismatch",
            account,
            [(first, replacement.encode() + b"\n"), (second, b"different-confirmation\n")],
            b"Passwords differ",
        )
        run_case("control_c", account, [(first, b"\x03")], b"Recovery canceled")
        run_case("terminal_eof", account, [(first, b"\x04")], b"Recovery canceled")
        run_case("wrong_owner", "synthetic-missing-owner", success_inputs, b"specified existing owner")
        with Worker():
            run_case("live_worker", account, success_inputs, b"worker holds")
        # Synthetic database admission controls only: no MCP server or Docker
        # invocation is substituted or launched by these negative cases.
        with sqlite3.connect(settings.database_path) as db:
            db.execute(
                "INSERT INTO accounts(id,username,password_hash,is_owner) VALUES ('synthetic-extra-owner','synthetic-extra-owner','invalid-synthetic-hash',1)"
            )
        baseline = state()
        run_case("ambiguous_owner", account, success_inputs, b"specified existing owner")
        with sqlite3.connect(settings.database_path) as db:
            db.execute("DELETE FROM accounts WHERE id='synthetic-extra-owner'")
            db.execute(
                "INSERT INTO tool_operations(account_id,operation_id,tool_name,tool_version,arguments_digest,status,journal_id) VALUES (?,'synthetic-pending','synthetic_mcp','1','synthetic','needs_reconciliation','synthetic-journal')",
                (account,),
            )
            db.execute(
                "INSERT INTO mcp_resources(name,identity_token,account_id,operation_id,image_reference,worker_owner) VALUES ('synthetic-unlaunched-resource','synthetic-token',?,'synthetic-pending','synthetic-not-an-image','synthetic-worker')",
                (account,),
            )
        baseline = state()
        run_case("pending_mcp_resource", account, success_inputs, b"Pending MCP resources")
        with sqlite3.connect(settings.database_path) as db:
            db.execute("DELETE FROM mcp_resources WHERE name='synthetic-unlaunched-resource'")
            db.execute("DELETE FROM tool_operations WHERE operation_id='synthetic-pending'")
        baseline = state()
        pipe = subprocess.run(
            [sys.executable, "-m", "app.owner_recovery", "--account-id", account],
            cwd=ROOT / "backend",
            env=environment,
            input=b"",
            capture_output=True,
            timeout=10,
        )
        checks["noninteractive_refused_without_mutation"] = (
            pipe.returncode != 0 and b"requires an interactive terminal" in pipe.stderr and state() == baseline
        )
        code, transcript, echo = terminal(account, environment, success_inputs)
        checks["actual_terminal_reset_succeeded"] = code == 0 and b"Owner credentials reset" in transcript
        checks["successful_input_echo_disabled"] = echo
        checks["successful_no_secret_output"] = all(
            value.encode() not in transcript for value in [password, replacement, old_token]
        )
        checks["successful_no_traceback"] = b"Traceback" not in transcript
        checks["old_password_rejected"] = asyncio.run(login(password)).status_code == 401
        checks["new_password_accepted"] = asyncio.run(login(replacement)).status_code == 200

        async def verify_state():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                checks["old_bearer_session_revoked"] = (
                    await client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {old_token}"})
                ).status_code == 401
            async with get_db() as db:
                note = await (
                    await db.execute("SELECT account_id,content FROM notes WHERE id='synthetic-note'")
                ).fetchone()
                checks["owner_id_and_content_preserved"] = tuple(note) == (account, "Keep this content")
                grant = await (
                    await db.execute("SELECT enabled,revision FROM tool_grants WHERE account_id=?", (account,))
                ).fetchone()
                checks["grant_disabled_revision_advanced"] = tuple(grant) == (0, 2)
                memory = await (
                    await db.execute(
                        "SELECT learning_enabled,retrieval_enabled,epoch FROM memory_settings WHERE account_id=?",
                        (account,),
                    )
                ).fetchone()
                checks["learning_and_retrieval_disabled"] = tuple(memory) == (0, 0, 1)
                rows = await (
                    await db.execute("SELECT data FROM journal_events WHERE account_id=?", (account,))
                ).fetchall()
                checks["one_durable_recovery_journal_event"] = (
                    len(rows) == 1 and json.loads(rows[0][0])["tool_name"] == "owner_recovery"
                )
                checks["journal_contains_no_credentials"] = all(
                    value not in str([tuple(row) for row in rows])
                    for value in [password, replacement, old_token, "argon2id"]
                )

        asyncio.run(verify_state())
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Real controlling PTY and actual app.owner_recovery subprocess, Argon2, SQLite and in-process ASGI auth; synthetic disposable data only. No live server, model inference, legacy migration or third-party integration demonstrated.",
    }
    EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    except (Exception, KeyboardInterrupt) as error:
        failure = {
            "passed": False,
            "error_type": type(error).__name__,
            "checked_at": datetime.now(UTC).isoformat(),
            "scope": "Terminal drill failed; no raw transcript or credentials recorded",
        }
        EVIDENCE.write_text(json.dumps(failure, indent=2) + "\n")
        print(json.dumps(failure))
        exit_code = 1
    raise SystemExit(exit_code)
