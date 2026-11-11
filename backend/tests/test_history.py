"""Actual SQLite history/cursor/isolation tests; no inference provider substituted."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.runs.service import append_event, claim, finish, submit_run
from app.runs.worker import Worker
from app.storage.database import get_db

ACCOUNT = "acc_sri_owner"
HEADERS = {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_history_watermark_partial_output_and_terminal_deduplication():
    saved = await submit_run(ACCOUNT, "Synthetic history cursor request", None, "local-ollama", "history-one")
    with Worker() as worker:
        run = await claim(worker.owner)
        await append_event(run, "token", {"token": "prefix "})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=HEADERS) as client:
            first = (await client.get(f"/api/v1/conversations/{saved['conversation_id']}/history")).json()
            assistant = first["items"][-1]
            assert assistant["content"] == "prefix " and assistant["status"] == "generating"
            cursor = assistant["event_seq"]
            await append_event(run, "token", {"token": "suffix"})
            await finish(run, "succeeded")
            response = await client.get(f"/api/v1/runs/{run['id']}/events", headers={"Last-Event-ID": str(cursor)})
            assert "suffix" in response.text and "prefix" not in response.text
            final = (await client.get(f"/api/v1/conversations/{saved['conversation_id']}/history")).json()
            assert len(final["items"]) == 2 and final["items"][-1]["content"] == "prefix suffix"
            assert final["items"][-1]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_history_preserves_canceled_partial_read():
    saved = await submit_run(ACCOUNT, "Synthetic canceled history", None, "local-ollama", "history-cancel")
    with Worker() as worker:
        run = await claim(worker.owner)
        await append_event(run, "token", {"token": "partial response"})
        await finish(run, "canceled")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=HEADERS) as client:
        data = (await client.get(f"/api/v1/conversations/{saved['conversation_id']}/history")).json()
        assert data["items"][-1]["content"] == "partial response" and data["items"][-1]["status"] == "canceled"


@pytest.mark.asyncio
async def test_owned_history_paginates_recent_messages_and_denies_known_other_id():
    async with get_db() as db:
        await db.execute(
            "INSERT INTO conversations (id,account_id,title,provider) VALUES ('own-history',?,'Synthetic saved history','local-ollama')",
            (ACCOUNT,),
        )
        for index in range(205):
            await db.execute(
                "INSERT INTO messages (id,conversation_id,account_id,role,content) VALUES (?,'own-history',?,'user',?)",
                (f"legacy-{index}", ACCOUNT, f"saved {index}"),
            )
        await db.execute(
            "INSERT INTO accounts (id,username,password_hash) VALUES ('other-history','other-history','test-only')"
        )
        await db.execute(
            "INSERT INTO conversations (id,account_id,title,provider) VALUES ('known-other-history','other-history','Private','local-ollama')"
        )
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=HEADERS) as client:
        recent = (await client.get("/api/v1/conversations/own-history/history")).json()
        assert len(recent["items"]) == 200 and recent["items"][0]["content"] == "saved 5"
        earlier = (
            await client.get(f"/api/v1/conversations/own-history/history?before={recent['older_before']}")
        ).json()
        assert len(earlier["items"]) == 5 and earlier["older_before"] is None
        assert len({item["id"] for item in earlier["items"] + recent["items"]}) == 205
        assert (await client.get("/api/v1/conversations/known-other-history/history")).status_code == 404
        assert "known-other-history" not in json.dumps((await client.get("/api/v1/conversations")).json())
