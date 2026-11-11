"""Local-only recovery. Stop this Archer API/worker and back up before use.

Run `uv run --locked python -m app.owner_recovery --account-id <owner-id>`.
Passwords are prompted twice, never accepted in arguments or environment.
"""

import argparse
import asyncio
import getpass
import json
import sys
import uuid
import warnings

from app.auth import hasher
from app.journal.service import emit_journal_event
from app.runs.worker import Worker
from app.storage.database import get_db


async def recover_owner(account_id: str, password: str) -> dict:
    """Reset exactly the existing owner, preserving IDs/content atomically."""
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must be 12–1024 characters")
    with Worker():
        password_hash = await asyncio.to_thread(hasher.hash, password)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            owners = await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchall()
            if len(owners) != 1 or owners[0]["id"] != account_id:
                raise ValueError("Expected exactly the specified existing owner; no account changed")
            pending = await (
                await db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','generating','cancel_requested')")
            ).fetchone()
            if pending[0]:
                raise ValueError("Unsettled runs exist; inspect/reconcile them before owner recovery")
            resources = await (await db.execute("SELECT COUNT(*) FROM mcp_resources WHERE status='pending'")).fetchone()
            if resources[0]:
                raise ValueError("Pending MCP resources require reconciliation before owner recovery")
            sessions = await db.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
            grants = await db.execute(
                "UPDATE tool_grants SET enabled=0,revision=revision+1 WHERE account_id=? AND enabled=1", (account_id,)
            )
            await db.execute("UPDATE accounts SET password_hash=? WHERE id=?", (password_hash, account_id))
            await db.execute(
                """INSERT INTO memory_settings(account_id,learning_enabled,retrieval_enabled,epoch)
                VALUES (?,0,0,1) ON CONFLICT(account_id) DO UPDATE SET
                learning_enabled=0,retrieval_enabled=0,epoch=epoch+1""",
                (account_id,),
            )
            await db.execute(
                "UPDATE memory_jobs SET status='canceled' WHERE account_id=? AND status='queued'", (account_id,)
            )
            await db.execute("DELETE FROM login_attempts")
            outcome = {
                "sessions_revoked": sessions.rowcount,
                "grants_disabled": grants.rowcount,
                "learning_enabled": False,
                "retrieval_enabled": False,
                "data_preserved": True,
            }
            entry_id = f"act_{uuid.uuid4().hex}"
            await db.execute(
                """INSERT INTO journal_entries(id,account_id,tool_name,arguments,permission_decision,outcome)
                VALUES (?,?,'owner_recovery','{}','local_filesystem',?)""",
                (entry_id, account_id, json.dumps(outcome)),
            )
            await emit_journal_event(db, entry_id)
            await db.commit()
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, help="Exact existing owner ID, inspected locally")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise SystemExit("Owner recovery requires an interactive terminal; no account changed")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("New owner password (at least 12 characters): ")
            if password != getpass.getpass("Confirm new password: "):
                raise SystemExit("Passwords differ; no account changed")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Recovery canceled; no account changed") from None
    except getpass.GetPassWarning:
        raise SystemExit("Echo-free password input unavailable; no account changed") from None
    try:
        result = asyncio.run(recover_owner(args.account_id, password))
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(result))
    print("Owner credentials reset. Sign in again; review grants and learning before re-enabling.")


if __name__ == "__main__":
    main()
