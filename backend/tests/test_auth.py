import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import provision_owner, token_digest
from app.journal.service import journal
from app.main import app
from app.storage.database import get_db


@pytest.mark.asyncio
async def test_session_lifecycle_and_boundaries():
    await provision_owner("owner", "a-long-test-password")
    with pytest.raises(ValueError, match="already provisioned"):
        await provision_owner("another", "a-long-test-password")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ["/api/notes", "/api/memories", "/api/journal", "/api/journal/stream", "/api/plugins"]:
            assert (
                await client.get(path, headers={"x-account-id": "acc_sri_owner", "account-id": "acc_sri_owner"})
            ).status_code == 401
        assert (
            await client.post("/api/auth/login", json={"username": "owner", "password": "wrong"})
        ).status_code == 401
        response = await client.post("/api/auth/login", json={"username": "owner", "password": "a-long-test-password"})
        assert response.status_code == 200
        token = response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "account-id": "acc_sri_owner"}
        async with get_db() as db:
            session = await (
                await db.execute("SELECT token FROM sessions WHERE token = ?", (token_digest(token),))
            ).fetchone()
            assert session and session["token"] != token
            await db.execute(
                "INSERT INTO conversations (id, account_id, title, provider) VALUES ('other-conv', 'acc_sri_owner', 'private', 'local-ollama')"
            )
            await db.commit()
        assert (await client.get("/api/notes", headers=headers)).status_code == 403
        assert (
            await client.put("/api/permissions/personal_notes/list", headers=headers, json={"enabled": True})
        ).status_code == 200
        assert (await client.get("/api/notes", headers=headers)).json() == []
        assert (
            await client.post(
                "/api/chat/stream", headers=headers, json={"message": "read history", "conversation_id": "other-conv"}
            )
        ).status_code == 404
        assert (
            await client.post("/api/chat/stream", headers=headers, json={"message": "hi", "provider": "deepseek"})
        ).status_code == 422
        assert (await client.post("/api/auth/logout", headers=headers)).status_code == 200
        assert (await client.get("/api/notes", headers=headers)).status_code == 401
        async with get_db() as db:
            await db.execute("UPDATE sessions SET expires_at = ?", (int(time.time()) - 1,))
            await db.commit()
        assert (await client.get("/api/notes", headers={"Authorization": "Bearer test-token"})).status_code == 401


@pytest.mark.asyncio
async def test_login_throttling_is_durable():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(10):
            assert (
                await client.post("/api/auth/login", json={"username": "missing", "password": "wrong"})
            ).status_code == 401
        assert (
            await client.post("/api/auth/login", json={"username": "missing", "password": "wrong"})
        ).status_code == 429


@pytest.mark.asyncio
async def test_journal_subscription_isolation():
    own = journal.subscribe("acc_sri_owner")
    other = journal.subscribe("other")
    try:
        await journal.log_action("acc_sri_owner", "test-only", {})
        assert own.qsize() == 1
        assert other.empty()
    finally:
        journal.unsubscribe(own)
        journal.unsubscribe(other)


@pytest.mark.asyncio
async def test_cookie_and_cross_origin_writes_cannot_authenticate():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/notes",
            json={"title": "injected"},
            headers={
                "Cookie": "session=test-token",
                "Origin": "https://attacker.invalid",
            },
        )
        assert response.status_code == 401
        response = await client.options(
            "/api/notes",
            headers={
                "Origin": "https://attacker.invalid",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_cloud_adapter_never_connects(monkeypatch):
    from app.cognition.models import ModelAdapter

    def forbidden_client(*args, **kwargs):
        raise AssertionError("Cloud selection must fail before opening an HTTP client")

    monkeypatch.setattr("app.cognition.models.httpx.AsyncClient", forbidden_client)
    with pytest.raises(ValueError, match="Cloud inference is disabled"):
        await ModelAdapter.chat_with_tools_step([], provider="deepseek")
    with pytest.raises(ValueError, match="Cloud inference is disabled"):
        async for _ in ModelAdapter.stream_chat([], provider="deepseek"):
            pass


@pytest.mark.asyncio
async def test_init_does_not_seed_a_bypass_owner():
    from app.storage.database import init_db

    async with get_db() as db:
        await db.execute("DELETE FROM accounts")
        await db.commit()
    await init_db()
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM accounts")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_legacy_owner_provisioning_preserves_records_and_revokes_sessions():
    async with get_db() as db:
        await db.execute("UPDATE accounts SET password_hash = 'local_dev_bypass', is_owner = 1")
        await db.execute(
            "INSERT INTO notes (id, account_id, title, content) VALUES ('legacy-note', 'acc_sri_owner', 'preserve', 'legacy')"
        )
        await db.commit()
    await provision_owner("new-owner", "a-long-test-password")
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM sessions")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT account_id FROM notes WHERE id = 'legacy-note'")).fetchone())[
            0
        ] == "acc_sri_owner"
        assert (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE is_owner = 1")).fetchone())[0] == 1
