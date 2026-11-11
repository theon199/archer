from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import token_digest
from app.journal.service import journal
from app.main import app
from app.runs.worker import Worker
from app.storage.database import get_db, init_db


@pytest.mark.asyncio
async def test_agent_tool_calling_loop():
    await init_db()

    # Clean existing journal entries for test and ensure account exists
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO accounts (id, username, password_hash) VALUES (?, ?, ?)",
            ("test_agent_user", "agent_tester", "hash"),
        )
        await db.execute("DELETE FROM journal_entries WHERE account_id = 'test_agent_user'")
        await db.execute("DELETE FROM notes WHERE account_id = 'test_agent_user'")
        await db.execute(
            "INSERT INTO sessions (id, account_id, token, expires_at) VALUES (?, ?, ?, ?)",
            ("agent-session", "test_agent_user", token_digest("agent-token"), 9999999999),
        )
        await db.execute(
            "INSERT INTO tool_grants (account_id,tool_name,action,tool_version,enabled) VALUES (?, ?, ?, ?, 1)",
            ("test_agent_user", "personal_notes", "create", "1"),
        )
        await db.commit()

    # Mock ModelAdapter.chat_with_tools_step to simulate model returning a structured tool call
    mock_step_result = {
        "role": "assistant",
        "content": "",
        "thinking": "The user wants to create a note. I will call personal_notes tool.",
        "tool_calls": [
            {
                "id": "call_123",
                "function": {
                    "name": "personal_notes",
                    "arguments": {
                        "action": "create",
                        "title": "Agent Milestone",
                        "content": "Explicitly mocked test execution",
                        "tags": ["spark", "archer"],
                    },
                },
            }
        ],
    }

    async def mock_stream_chat(messages, system_prompt=None, provider=None):
        yield "I have created the note 'Agent Milestone' in this mocked test."

    with patch("app.cognition.models.ModelAdapter.chat_with_tools_step", return_value=mock_step_result):
        with patch("app.cognition.models.ModelAdapter.stream_chat", side_effect=mock_stream_chat):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat/stream",
                    json={"message": "Please make a note titled Agent Milestone"},
                    headers={"Authorization": "Bearer agent-token"},
                )
                assert resp.status_code == 200

                with Worker() as worker:
                    assert await worker.run_one()
                replay = await client.get(
                    f"/api/v1/runs/{resp.json()['run_id']}/events", headers={"Authorization": "Bearer agent-token"}
                )
                body = replay.text
                assert "tool_call" in body
                assert "personal_notes" in body
                assert "tool_result" in body
                assert "Agent Milestone" in body

    # Verify that the note was created in database
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM notes WHERE account_id = 'test_agent_user' AND title = 'Agent Milestone'"
        ) as cursor:
            note = await cursor.fetchone()
            assert note is not None
            assert note["title"] == "Agent Milestone"

    # Verify that the action was recorded in the Action Journal
    history = await journal.get_history(account_id="test_agent_user")
    assert len(history) >= 1
    latest = history[0]
    assert latest["tool_name"] == "personal_notes"
    assert latest["permission_decision"] == "allowed"
    assert latest["arguments"]["action"] == "create"
    assert latest["outcome"]["success"] is True
