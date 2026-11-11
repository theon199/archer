"""Database storage layer using aiosqlite."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aiosqlite

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_status (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, heartbeat REAL NOT NULL);

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

CREATE TABLE IF NOT EXISTS login_attempts (attempted_at INTEGER NOT NULL);

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

-- Durable queue: each run is also its single foreground job record.
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_owner ON conversations(id, account_id);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    client_operation_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    message TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    output TEXT NOT NULL DEFAULT '',
    error TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    tool_pending INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, client_operation_id),
    UNIQUE(id, account_id),
    FOREIGN KEY(conversation_id, account_id) REFERENCES conversations(id, account_id)
);
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(run_id, seq),
    FOREIGN KEY(run_id, account_id) REFERENCES runs(id, account_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_runs_queue ON runs(status, created_at);

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

CREATE UNIQUE INDEX IF NOT EXISTS uq_note_owner ON notes(id, account_id);
CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS tool_grants (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL, action TEXT NOT NULL, tool_version TEXT NOT NULL,
    enabled INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(account_id, tool_name, action)
);
CREATE TABLE IF NOT EXISTS tool_operations (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL, tool_name TEXT NOT NULL, tool_version TEXT NOT NULL,
    arguments_digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
    run_id TEXT, journal_id TEXT NOT NULL,
    PRIMARY KEY(account_id, operation_id)
);
CREATE TABLE IF NOT EXISTS mcp_resources (
    name TEXT PRIMARY KEY, identity_token TEXT NOT NULL,
    account_id TEXT NOT NULL, operation_id TEXT NOT NULL,
    image_reference TEXT NOT NULL, worker_owner TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(account_id,operation_id) REFERENCES tool_operations(account_id,operation_id)
);
CREATE TABLE IF NOT EXISTS note_revisions (
    note_id TEXT NOT NULL, account_id TEXT NOT NULL, revision INTEGER NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL,
    deleted_at TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(note_id, revision),
    FOREIGN KEY(note_id, account_id) REFERENCES notes(id, account_id)
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_owner ON memories(id, account_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_message_owner ON messages(id, account_id);
CREATE TABLE IF NOT EXISTS memory_settings (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    learning_enabled INTEGER NOT NULL DEFAULT 1,
    retrieval_enabled INTEGER NOT NULL DEFAULT 1,
    epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory_jobs (
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    PRIMARY KEY(account_id, message_id),
    FOREIGN KEY(message_id, account_id) REFERENCES messages(id, account_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS memory_evidence (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_message_id TEXT,
    source_kind TEXT NOT NULL,
    value TEXT NOT NULL,
    quote TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(memory_id, account_id) REFERENCES memories(id, account_id) ON DELETE CASCADE,
    FOREIGN KEY(source_message_id, account_id) REFERENCES messages(id, account_id)
);
CREATE TABLE IF NOT EXISTS memory_suppressions (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    subject_key TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(account_id, subject_key)
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

CREATE TABLE IF NOT EXISTS journal_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    journal_id TEXT NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_events_account ON journal_events(account_id, seq);

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
    """Initialize database tables without creating implicit accounts."""
    async with get_db() as db:
        await db.executescript(SCHEMA)
        await db.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in await (await db.execute("PRAGMA table_info(notes)")).fetchall()}
        if "revision" not in columns:
            await db.execute("ALTER TABLE notes ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        if "deleted_at" not in columns:
            await db.execute("ALTER TABLE notes ADD COLUMN deleted_at TEXT")
        await db.execute(
            "INSERT OR IGNORE INTO note_revisions (note_id, account_id, revision, title, content, tags, deleted_at) SELECT id, account_id, revision, title, content, tags, deleted_at FROM notes"
        )
        await db.execute(
            "INSERT OR IGNORE INTO schema_migrations (version) VALUES ('003_tool_permissions_note_revisions')"
        )
        memory_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(memories)")).fetchall()}
        for name, definition in {
            "subject_key": "TEXT",
            "value": "TEXT",
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "learning_epoch": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in memory_columns:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_memory_subject ON memories(account_id,subject_key) WHERE subject_key IS NOT NULL AND validity=1"
        )
        await db.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES ('004_memory_evidence_epochs')")
        await db.commit()
