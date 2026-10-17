"""Database storage layer using aiosqlite."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aiosqlite

from app.config import settings

SCHEMA = """
-- Accounts
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_owner INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sessions
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Conversations
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    title TEXT NOT NULL,
    provider TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Messages
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Personal Notes
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Learned Memories
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,  -- 'preference' | 'routine_suggestion' | 'fact'
    content TEXT NOT NULL,
    evidence TEXT NOT NULL,     -- attributable quotes or message reference
    origin TEXT DEFAULT 'inferred', -- 'inferred' | 'explicit' | 'corrected'
    validity INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Action Journal
CREATE TABLE IF NOT EXISTS journal_entries (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    conversation_id TEXT,
    run_id TEXT,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    permission_decision TEXT NOT NULL, -- 'allowed' | 'denied' | 'pending'
    outcome TEXT,
    error TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_conversations_account ON conversations(account_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_notes_account ON notes(account_id);
CREATE INDEX IF NOT EXISTS idx_memories_account ON memories(account_id);
CREATE INDEX IF NOT EXISTS idx_journal_account ON journal_entries(account_id);
"""


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Provide an asynchronous connection to the SQLite database."""
    db_path = settings.database_path
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.execute("PRAGMA journal_mode = WAL;")
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Initialize database tables and default developer account if empty."""
    async with get_db() as db:
        await db.executescript(SCHEMA)
        await db.commit()

        # Seed default owner account if table is empty
        async with db.execute("SELECT COUNT(*) as count FROM accounts") as cursor:
            row = await cursor.fetchone()
            if row and row["count"] == 0:
                await db.execute(
                    """
                    INSERT INTO accounts (id, username, password_hash, is_owner)
                    VALUES (?, ?, ?, 1)
                    """,
                    (
                        "acc_sri_owner",
                        "sri",
                        "local_dev_bypass",
                    ),
                )
                await db.commit()
