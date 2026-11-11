"""Authenticated durable run submission, snapshots, cancellation and SSE replay."""

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.auth import current_account
from app.runs.service import TERMINAL, cancel_run, events_after, get_run, submit_run
from app.storage.database import get_db

router = APIRouter()


class RunCreate(BaseModel):
    message: str = Field(min_length=1, max_length=32000)
    client_operation_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    provider: str | None = None


@router.post("/runs", status_code=202)
async def create_run(body: RunCreate, account_id: str = Depends(current_account)):
    run = await submit_run(account_id, body.message, body.conversation_id, body.provider, body.client_operation_id)
    return snapshot(run)


@router.post("/conversations/{conversation_id}/runs", status_code=202)
async def create_conversation_run(conversation_id: str, body: RunCreate, account_id: str = Depends(current_account)):
    if body.conversation_id and body.conversation_id != conversation_id:
        raise HTTPException(422, "Conversation IDs differ")
    return snapshot(
        await submit_run(account_id, body.message, conversation_id, body.provider, body.client_operation_id)
    )


def snapshot(run):
    return {key: run[key] for key in ("id", "conversation_id", "status", "output", "error", "created_at", "updated_at")}


@router.get("/runs/{run_id}")
async def read_run(run_id: str, account_id: str = Depends(current_account)):
    return snapshot(await get_run(account_id, run_id))


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: str, account_id: str = Depends(current_account)):
    return snapshot(await cancel_run(account_id, run_id))


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
    authorization: str = Header(default=""),
    account_id: str = Depends(current_account),
):
    await get_run(account_id, run_id)
    if last_event_id is not None:
        try:
            after = int(last_event_id)
            if after < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Last-Event-ID must be a nonnegative sequence number") from None

    async def replay():
        cursor = after
        while True:
            # Expiration and logout take effect on an already-open connection.
            try:
                await current_account(authorization)
            except HTTPException:
                yield {"event": "auth_expired", "data": "{}"}
                return
            rows = await events_after(account_id, run_id, cursor)
            for row in rows:
                cursor = row["seq"]
                yield {"id": str(cursor), "event": row["event"], "data": row["data"]}
            if len(rows) < 200:
                run = await get_run(account_id, run_id)
                if run["status"] in TERMINAL:
                    # Recheck after observing terminal state to close a commit race.
                    if not await events_after(account_id, run_id, cursor):
                        return
            await asyncio.sleep(0.1)

    return EventSourceResponse(replay(), send_timeout=30, headers={"Cache-Control": "no-store"})


@router.get("/conversations")
async def conversations(account_id: str = Depends(current_account)):
    async with get_db() as db:
        rows = await (
            await db.execute(
                "SELECT id, title, provider, created_at, updated_at FROM conversations WHERE account_id = ? ORDER BY rowid DESC LIMIT 100",
                (account_id,),
            )
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/conversations/{conversation_id}/messages")
async def messages(conversation_id: str, account_id: str = Depends(current_account)):
    async with get_db() as db:
        owner = await (
            await db.execute(
                "SELECT id FROM conversations WHERE id = ? AND account_id = ?", (conversation_id, account_id)
            )
        ).fetchone()
        if owner is None:
            raise HTTPException(404, "Conversation not found")
        rows = await (
            await db.execute(
                "SELECT id, role, content, created_at FROM messages WHERE conversation_id = ? AND account_id = ? ORDER BY rowid LIMIT 500",
                (conversation_id, account_id),
            )
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/conversations/{conversation_id}/history")
async def history(
    conversation_id: str, before: int | None = Query(default=None, ge=1), account_id: str = Depends(current_account)
):
    """Owned recent history, including unfinished/failed partial output and replay cursor.

    One SELECT observes run output and event watermark from the same SQLite snapshot.
    Legacy messages remain visible. Pagination uses immutable message row order.
    """
    async with get_db() as db:
        conversation = await (
            await db.execute(
                "SELECT id,title FROM conversations WHERE id=? AND account_id=?", (conversation_id, account_id)
            )
        ).fetchone()
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        rows = await (
            await db.execute(
                """
            SELECT m.id,m.role,m.content,m.rowid AS history_seq,r.id AS run_id,
                r.output,r.status,r.error,
                COALESCE((SELECT MAX(seq) FROM run_events WHERE run_id=r.id),0) AS event_seq
            FROM messages m LEFT JOIN runs r ON r.account_id=m.account_id AND
                (m.id='user_'||r.id OR m.id='assistant_'||r.id)
            WHERE m.conversation_id=? AND m.account_id=? AND (? IS NULL OR m.rowid<?)
            ORDER BY m.rowid DESC LIMIT 201
        """,
                (conversation_id, account_id, before, before),
            )
        ).fetchall()
    more = len(rows) > 200
    rows = list(reversed(rows[:200]))
    users = {row["run_id"] for row in rows if row["role"] == "user" and row["run_id"]}
    items = []
    for row in rows:
        run_id = row["run_id"]
        if run_id and row["role"] == "assistant" and run_id in users:
            continue
        if not run_id or row["role"] == "user":
            items.append({"id": row["id"], "role": row["role"], "content": row["content"]})
        if run_id:
            items.append(
                {
                    "id": "assistant_" + run_id,
                    "role": "assistant",
                    "content": row["output"],
                    "run_id": run_id,
                    "status": row["status"],
                    "error": row["error"],
                    "event_seq": row["event_seq"],
                }
            )
    return {
        "conversation": dict(conversation),
        "items": items,
        "older_before": rows[0]["history_seq"] if more else None,
    }
