"""Durable single-host foreground jobs and monotonically sequenced replay."""

import hashlib
import json
import time
import uuid

from fastapi import HTTPException

from app.config import settings
from app.memory.extractor import extractor
from app.storage.database import get_db

TERMINAL = {"succeeded", "failed", "canceled", "needs_reconciliation"}
ACTIVE = {"generating", "cancel_requested"}
QUEUE_LIMIT = 20


async def event_in_transaction(db, run, event, data):
    if len(json.dumps(data).encode()) > 1048576:
        raise RuntimeError("Event size limit exceeded")
    seq = (
        await (
            await db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?", (run["id"],))
        ).fetchone()
    )[0]
    await db.execute(
        "INSERT INTO run_events (run_id, account_id, seq, event, data) VALUES (?, ?, ?, ?, ?)",
        (run["id"], run["account_id"], seq, event, json.dumps(data)),
    )


async def submit_run(account_id, message, conversation_id, provider, operation_id):
    provider = provider or settings.model_provider
    if provider not in {"local-ollama", "local-sglang"}:
        raise HTTPException(422, "Cloud inference is disabled")
    if not message.strip() or len(message) > 32000 or not 1 <= len(operation_id) <= 128:
        raise HTTPException(422, "Invalid message or client operation ID")
    digest = hashlib.sha256(json.dumps([message, conversation_id, provider]).encode()).hexdigest()
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        previous = await (
            await db.execute(
                "SELECT * FROM runs WHERE account_id = ? AND client_operation_id = ?", (account_id, operation_id)
            )
        ).fetchone()
        if previous:
            if previous["request_digest"] != digest:
                raise HTTPException(409, "Client operation ID was already used with different input")
            return dict(previous)
        count = (
            await (
                await db.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'generating', 'cancel_requested')"
                )
            ).fetchone()
        )[0]
        if count >= QUEUE_LIMIT:
            raise HTTPException(429, "Run queue is full")
        conv_id = conversation_id or "conv_" + uuid.uuid4().hex
        await db.execute(
            "INSERT OR IGNORE INTO conversations (id, account_id, title, provider) VALUES (?, ?, ?, ?)",
            (conv_id, account_id, message[:60], provider),
        )
        owner = await (await db.execute("SELECT account_id FROM conversations WHERE id = ?", (conv_id,))).fetchone()
        if owner["account_id"] != account_id:
            raise HTTPException(404, "Conversation not found")
        run = {"id": "run_" + uuid.uuid4().hex, "account_id": account_id, "conversation_id": conv_id}
        await db.execute(
            "INSERT INTO runs (id, account_id, conversation_id, client_operation_id, request_digest, message, provider) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run["id"], account_id, conv_id, operation_id, digest, message, provider),
        )
        await db.execute(
            "INSERT INTO messages (id, conversation_id, account_id, role, content) VALUES (?, ?, ?, 'user', ?)",
            ("user_" + run["id"], conv_id, account_id, message),
        )
        await event_in_transaction(db, run, "state", {"status": "queued"})
        await db.commit()
    return await get_run(account_id, run["id"])


async def get_run(account_id, run_id):
    async with get_db() as db:
        row = await (
            await db.execute("SELECT * FROM runs WHERE id = ? AND account_id = ?", (run_id, account_id))
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Run not found")
    return dict(row)


async def events_after(account_id, run_id, after):
    await get_run(account_id, run_id)
    async with get_db() as db:
        rows = await (
            await db.execute(
                "SELECT seq, event, data FROM run_events WHERE run_id = ? AND account_id = ? AND seq > ? ORDER BY seq LIMIT 200",
                (run_id, account_id, after),
            )
        ).fetchall()
    return [dict(row) for row in rows]


async def cancel_run(account_id, run_id):
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (
            await db.execute("SELECT * FROM runs WHERE id = ? AND account_id = ?", (run_id, account_id))
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Run not found")
        if row["status"] not in TERMINAL and row["status"] != "cancel_requested":
            state = "canceled" if row["status"] == "queued" else "cancel_requested"
            await db.execute("UPDATE runs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (state, run_id))
            await event_in_transaction(db, row, "state", {"status": state})
            if state == "canceled":
                await event_in_transaction(db, row, "done", {"status": state})
        await db.commit()
    return await get_run(account_id, run_id)


async def claim(owner):
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        busy = await (
            await db.execute("SELECT id FROM runs WHERE status IN ('generating', 'cancel_requested') LIMIT 1")
        ).fetchone()
        if busy:
            return None
        row = await (await db.execute("SELECT * FROM runs WHERE status = 'queued' ORDER BY rowid LIMIT 1")).fetchone()
        if row is None:
            return None
        await db.execute(
            "UPDATE runs SET status = 'generating', generation = generation + 1, lease_owner = ?, lease_until = ? WHERE id = ?",
            (owner, time.time() + 15, row["id"]),
        )
        await event_in_transaction(db, row, "state", {"status": "generating"})
        await db.commit()
    return await get_run(row["account_id"], row["id"])


async def append_event(run, event, data):
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT * FROM runs WHERE id = ?", (run["id"],))).fetchone()
        if (
            row["lease_owner"] != run["lease_owner"]
            or row["generation"] != run["generation"]
            or row["status"] != "generating"
            or row["lease_until"] < time.time()
        ):
            raise RuntimeError("Run lease lost or cancellation requested")
        output = row["output"] + (data.get("token", "") if event == "token" else "")
        if len(output) > 128000:
            raise RuntimeError("Output limit exceeded")
        pending = 1 if event == "tool_call" else 0 if event == "tool_result" else row["tool_pending"]
        await db.execute(
            "UPDATE runs SET output = ?, tool_pending = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (output, pending, run["id"]),
        )
        await event_in_transaction(db, run, event, data)
        await db.commit()


async def finish(run, status, error=None, *, recovery=False):
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT * FROM runs WHERE id = ?", (run["id"],))).fetchone()
        if (
            row["status"] in TERMINAL
            or row["generation"] != run["generation"]
            or row["lease_owner"] != run["lease_owner"]
        ):
            return
        if row["tool_pending"]:
            status = "needs_reconciliation"
        elif row["status"] == "cancel_requested":
            status = "canceled"
        if status == "succeeded":
            await db.execute(
                "INSERT INTO messages (id, conversation_id, account_id, role, content) VALUES (?, ?, ?, 'assistant', ?)",
                ("assistant_" + run["id"], row["conversation_id"], row["account_id"], row["output"]),
            )
        await db.execute(
            "UPDATE runs SET status = ?, error = ?, lease_owner = NULL, lease_until = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, error, run["id"]),
        )
        await event_in_transaction(db, run, "state", {"status": status, "error": error})
        await event_in_transaction(db, run, "done", {"status": status})
        if recovery:
            from app.plugins.registry import record

            await record(
                db,
                row["account_id"],
                "run_recovery",
                {"action": "worker_interrupted", "had_pending_tool": bool(row["tool_pending"])},
                "allowed",
                {
                    "success": False,
                    "status": status,
                    "error": "Worker interrupted; inspect recorded tool outcomes; no automatic retry",
                },
                row["id"],
                row["conversation_id"],
            )
        if status == "succeeded":
            await extractor.enqueue_in_transaction(db, row["account_id"], "user_" + run["id"])
        await db.commit()


async def recover():
    """Call only while holding the worker lock: the previous executor is stopped.

    Never replay a previously running operation. Preserve its partial output and
    mark any outstanding tool intent as needing human reconciliation.
    """
    async with get_db() as db:
        rows = await (
            await db.execute("SELECT * FROM runs WHERE status IN ('generating', 'cancel_requested')")
        ).fetchall()
    for row in rows:
        await finish(dict(row), "failed", "Worker interrupted; no automatic retry", recovery=True)
        # A crash may occur after tool intent commits but before a container is
        # reserved. Reconcile every interrupted operation, not only resource rows.
        from app.plugins.registry import record

        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            operations = await (
                await db.execute(
                    "SELECT * FROM tool_operations WHERE account_id=? AND run_id=? AND status='running' AND result IS NULL",
                    (row["account_id"], row["id"]),
                )
            ).fetchall()
            for operation in operations:
                await db.execute(
                    "UPDATE tool_operations SET status='needs_reconciliation' WHERE account_id=? AND operation_id=?",
                    (row["account_id"], operation["operation_id"]),
                )
                old = await (
                    await db.execute(
                        "SELECT outcome FROM journal_entries WHERE id=? AND account_id=?",
                        (operation["journal_id"], row["account_id"]),
                    )
                ).fetchone()
                outcome = json.loads(old["outcome"]) if old else {}
                outcome.update(
                    success=False,
                    status="needs_reconciliation",
                    error="Worker interrupted; original tool outcome unknown",
                )
                await record(
                    db,
                    row["account_id"],
                    operation["tool_name"],
                    {},
                    "allowed",
                    outcome,
                    row["id"],
                    row["conversation_id"],
                    operation["journal_id"],
                )
            await db.commit()
