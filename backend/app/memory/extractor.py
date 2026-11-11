"""Curated, evidence-backed learning. No model/tool text can supply provenance."""

import hashlib
import json
import re
import uuid

from fastapi import HTTPException

from app.journal.service import emit_journal_event
from app.storage.database import get_db

VALUES = {
    "response_length": {"concise": "Keep responses concise.", "detailed": "Give detailed responses."},
    "response_format": {"bullets": "Prefer bullet points.", "paragraphs": "Prefer paragraphs."},
    "units": {"metric": "Prefer metric units.", "imperial": "Prefer imperial units."},
    "daily_planning_routine": {
        "review": "Suggested routine: review commitments and choose daily priorities. This is a suggestion only; nothing is scheduled."
    },
}
PATTERNS = [
    (r"i prefer (concise|short|detailed) (?:answers|responses)[.!]?", "response_length"),
    (r"i prefer (bullet points|paragraphs)[.!]?", "response_format"),
    (r"i prefer (metric|imperial) units[.!]?", "units"),
    (r"(?:please )?suggest a daily planning routine[.!]?", "daily_planning_routine"),
]


def candidate(content):
    # Full-message matching deliberately rejects quoted instructions, pasted tool
    # output, credentials, sensitive traits and arbitrary executable preferences.
    text = content.strip().casefold()
    for pattern, key in PATTERNS:
        match = re.fullmatch(pattern, text)
        if match:
            value = match.group(1) if match.lastindex else "review"
            value = {"short": "concise", "bullet points": "bullets"}.get(value, value)
            return key, value
    return None


async def ensure_settings(db, account_id):
    row = await (await db.execute("SELECT * FROM memory_settings WHERE account_id=?", (account_id,))).fetchone()
    if row is not None:
        return row
    await db.execute("INSERT OR IGNORE INTO memory_settings (account_id) VALUES (?)", (account_id,))
    return await (await db.execute("SELECT * FROM memory_settings WHERE account_id=?", (account_id,))).fetchone()


async def invalidate(db, account_id):
    await db.execute("UPDATE memory_settings SET epoch=epoch+1 WHERE account_id=?", (account_id,))
    await db.execute(
        "UPDATE memory_jobs SET status='invalidated' WHERE account_id=? AND status='queued'", (account_id,)
    )


async def audit(db, account_id, action, memory_id=None):
    entry = "act_" + uuid.uuid4().hex
    await db.execute(
        "INSERT INTO journal_entries (id,account_id,tool_name,arguments,permission_decision,outcome) VALUES (?,?,?,?,?,?)",
        (
            entry,
            account_id,
            "memory_control",
            json.dumps({"action": action, "memory_id": memory_id}),
            "allowed",
            json.dumps({"success": True, "status": action}),
        ),
    )
    await emit_journal_event(db, entry)


class MemoryExtractor:
    async def enqueue_in_transaction(self, db, account_id, message_id):
        settings = await ensure_settings(db, account_id)
        if not settings["learning_enabled"]:
            return
        row = await (
            await db.execute(
                "SELECT content FROM messages WHERE id=? AND account_id=? AND role='user'", (message_id, account_id)
            )
        ).fetchone()
        if not row or not candidate(row["content"]):
            return
        count = (await (await db.execute("SELECT COUNT(*) FROM memory_jobs WHERE status='queued'")).fetchone())[0]
        if count < 100:
            await db.execute(
                "INSERT OR IGNORE INTO memory_jobs (account_id,message_id,epoch) VALUES (?,?,?)",
                (account_id, message_id, settings["epoch"]),
            )

    async def process_one(self):
        # Fast, bounded local SQL/rule work; no inference or network operation.
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if await (
                await db.execute(
                    "SELECT id FROM runs WHERE status IN ('queued','generating','cancel_requested') LIMIT 1"
                )
            ).fetchone():
                return False
            job = await (
                await db.execute("SELECT * FROM memory_jobs WHERE status='queued' ORDER BY rowid LIMIT 1")
            ).fetchone()
            if job is None:
                return False
            settings = await ensure_settings(db, job["account_id"])
            if not settings["learning_enabled"] or settings["epoch"] != job["epoch"]:
                await db.execute(
                    "UPDATE memory_jobs SET status='invalidated' WHERE account_id=? AND message_id=?",
                    (job["account_id"], job["message_id"]),
                )
            else:
                await self.extract_in_transaction(db, job["account_id"], job["message_id"], job["epoch"])
                await db.execute(
                    "UPDATE memory_jobs SET status='done' WHERE account_id=? AND message_id=?",
                    (job["account_id"], job["message_id"]),
                )
            await db.commit()
            return True

    async def extract_in_transaction(self, db, account_id, message_id, epoch):
        settings = await ensure_settings(db, account_id)
        if not settings["learning_enabled"] or settings["epoch"] != epoch:
            return None
        source = await (
            await db.execute(
                "SELECT content FROM messages WHERE id=? AND account_id=? AND role='user'", (message_id, account_id)
            )
        ).fetchone()
        parsed = candidate(source["content"]) if source else None
        if parsed is None:
            return None
        key, value = parsed
        if await (
            await db.execute(
                "SELECT 1 FROM memory_suppressions WHERE account_id=? AND subject_key=?", (account_id, key)
            )
        ).fetchone():
            return None
        previous = await (
            await db.execute(
                "SELECT * FROM memories WHERE account_id=? AND subject_key=? AND validity=1", (account_id, key)
            )
        ).fetchone()
        if previous and previous["origin"] == "corrected":
            return dict(previous)
        # Deduplicate evidence; a restarted job cannot create repeated revisions.
        if await (
            await db.execute(
                "SELECT 1 FROM memory_evidence WHERE account_id=? AND source_message_id=?", (account_id, message_id)
            )
        ).fetchone():
            return dict(previous) if previous else None
        mem_id = previous["id"] if previous else "mem_" + uuid.uuid4().hex
        memory_type = "routine_suggestion" if key == "daily_planning_routine" else "preference"
        if previous:
            await db.execute(
                "UPDATE memories SET content=?,value=?,evidence=?,revision=revision+1,learning_epoch=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND account_id=?",
                (VALUES[key][value], value, message_id, epoch, mem_id, account_id),
            )
        else:
            await db.execute(
                "INSERT INTO memories (id,account_id,memory_type,content,evidence,origin,subject_key,value,learning_epoch) VALUES (?,?,?,?,?,'inferred',?,?,?)",
                (mem_id, account_id, memory_type, VALUES[key][value], message_id, key, value, epoch),
            )
        await db.execute(
            "INSERT INTO memory_evidence (id,memory_id,account_id,source_message_id,source_kind,value,quote) VALUES (?,?,?,?,'user_message',?,?)",
            (uuid.uuid4().hex, mem_id, account_id, message_id, value, source["content"]),
        )
        await audit(db, account_id, "learned", mem_id)
        return {"id": mem_id, "subject_key": key, "value": value}

    async def get_memories(self, account_id):
        async with get_db() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM memories WHERE account_id=? AND validity=1 ORDER BY rowid DESC LIMIT 200",
                    (account_id,),
                )
            ).fetchall()
            result = []
            for row in rows:
                evidence = await (
                    await db.execute(
                        "SELECT source_message_id,source_kind,value,quote,created_at FROM memory_evidence WHERE memory_id=? AND account_id=? ORDER BY rowid",
                        (row["id"], account_id),
                    )
                ).fetchall()
                result.append(
                    {
                        **dict(row),
                        "sources": [dict(e) for e in evidence],
                        "allowed_values": list(VALUES.get(row["subject_key"], {})),
                    }
                )
            return result

    async def preferences_for_context(self, account_id):
        async with get_db() as db:
            settings = await (
                await db.execute("SELECT * FROM memory_settings WHERE account_id=?", (account_id,))
            ).fetchone()
            if settings and not settings["retrieval_enabled"]:
                return []
            rows = await (
                await db.execute(
                    "SELECT m.* FROM memories m WHERE m.account_id=? AND m.validity=1 AND m.memory_type='preference' AND NOT EXISTS (SELECT 1 FROM memory_suppressions s WHERE s.account_id=m.account_id AND s.subject_key=m.subject_key) AND EXISTS (SELECT 1 FROM memory_evidence e WHERE e.memory_id=m.id AND e.account_id=m.account_id) ORDER BY CASE m.origin WHEN 'corrected' THEN 0 ELSE 1 END, m.rowid DESC LIMIT 20",
                    (account_id,),
                )
            ).fetchall()
        return [
            VALUES[row["subject_key"]][row["value"]]
            for row in rows
            if row["subject_key"] in VALUES and row["value"] in VALUES[row["subject_key"]]
        ]

    async def correct_memory(self, account_id, memory_id, new_content, expected_revision):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_settings(db, account_id)
            row = await (
                await db.execute(
                    "SELECT * FROM memories WHERE id=? AND account_id=? AND validity=1", (memory_id, account_id)
                )
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Memory not found")
            if row["revision"] != expected_revision:
                raise HTTPException(409, "Memory revision changed")
            if new_content not in VALUES.get(row["subject_key"], {}):
                raise HTTPException(422, "Unsupported preference value; inspect allowed_values")
            await invalidate(db, account_id)
            settings = await ensure_settings(db, account_id)
            await db.execute(
                "UPDATE memories SET content=?,value=?,origin='corrected',revision=revision+1,learning_epoch=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND account_id=?",
                (VALUES[row["subject_key"]][new_content], new_content, settings["epoch"], memory_id, account_id),
            )
            await db.execute(
                "INSERT INTO memory_evidence (id,memory_id,account_id,source_kind,value,quote) VALUES (?,?,?,'correction',?,?)",
                (uuid.uuid4().hex, memory_id, account_id, new_content, new_content),
            )
            await audit(db, account_id, "corrected", memory_id)
            await db.commit()
        return True

    async def delete_memory(self, account_id, memory_id, expected_revision):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await ensure_settings(db, account_id)
            row = await (
                await db.execute(
                    "SELECT * FROM memories WHERE id=? AND account_id=? AND validity=1", (memory_id, account_id)
                )
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Memory not found")
            if row["revision"] != expected_revision:
                raise HTTPException(409, "Memory revision changed")
            key = row["subject_key"] or "legacy:" + memory_id
            digest = hashlib.sha256(row["evidence"].encode()).hexdigest()
            await db.execute(
                "INSERT OR REPLACE INTO memory_suppressions (account_id,subject_key,source_digest) VALUES (?,?,?)",
                (account_id, key, digest),
            )
            await invalidate(db, account_id)
            await db.execute("DELETE FROM memories WHERE id=? AND account_id=?", (memory_id, account_id))
            await audit(db, account_id, "deleted", memory_id)
            await db.commit()
        return True

    async def settings(self, account_id):
        async with get_db() as db:
            row = await ensure_settings(db, account_id)
            suppressions = await (
                await db.execute(
                    "SELECT subject_key,created_at FROM memory_suppressions WHERE account_id=?", (account_id,)
                )
            ).fetchall()
            await db.commit()
        return {**dict(row), "suppressed_topics": [dict(s) for s in suppressions], "supported_values": VALUES}

    async def change_settings(self, account_id, learning_enabled, retrieval_enabled, expected_epoch=None):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await ensure_settings(db, account_id)
            if expected_epoch is not None and row["epoch"] != expected_epoch:
                raise HTTPException(409, "Learning settings changed; refresh before retrying")
            if row["learning_enabled"] != learning_enabled or row["retrieval_enabled"] != retrieval_enabled:
                await invalidate(db, account_id)
                await db.execute(
                    "UPDATE memory_settings SET learning_enabled=?,retrieval_enabled=? WHERE account_id=?",
                    (learning_enabled, retrieval_enabled, account_id),
                )
                await audit(db, account_id, "settings_changed")
            await db.commit()
        return await self.settings(account_id)


extractor = MemoryExtractor()
