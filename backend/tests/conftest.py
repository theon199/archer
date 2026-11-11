"""Tests must never write into the user's runtime database."""

import time

import pytest_asyncio

from app.auth import token_digest
from app.config import settings
from app.storage.database import get_db, init_db


@pytest_asyncio.fixture(autouse=True)
async def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "brave_search_api_key", "")
    monkeypatch.setattr(settings, "tavily_api_key_file", None)
    monkeypatch.setattr(settings, "tools_default_enabled", False)
    await init_db()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO accounts (id, username, password_hash) VALUES ('acc_sri_owner', 'test-owner', 'test-only-invalid-hash')"
        )
        await db.execute(
            "INSERT INTO sessions (id, account_id, token, expires_at) VALUES (?, ?, ?, ?)",
            ("test-session", "acc_sri_owner", token_digest("test-token"), int(time.time()) + 3600),
        )
        await db.commit()
