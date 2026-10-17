"""Learned Memory Extractor - Automatic preference learning with evidence and provenance."""

import logging
import uuid
from typing import Any

from app.storage.database import get_db

logger = logging.getLogger(__name__)


class MemoryExtractor:
    """Extracts, updates, and retrieves inspectable user preferences and routine suggestions."""

    @staticmethod
    async def extract_and_save_preference(
        account_id: str,
        content: str,
        evidence: str,
        memory_type: str = "preference",
        origin: str = "inferred",
    ) -> dict[str, Any]:
        """Save a learned preference with supporting evidence."""
        mem_id = f"mem_{uuid.uuid4().hex[:8]}"
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO memories (id, account_id, memory_type, content, evidence, origin, validity)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (mem_id, account_id, memory_type, content, evidence, origin),
            )
            await db.commit()
            return {
                "id": mem_id,
                "account_id": account_id,
                "memory_type": memory_type,
                "content": content,
                "evidence": evidence,
                "origin": origin,
                "validity": 1,
            }

    @staticmethod
    async def get_memories(account_id: str) -> list[dict[str, Any]]:
        """Retrieve all active learned memories for an account."""
        async with get_db() as db:
            async with db.execute(
                """
                SELECT id, memory_type, content, evidence, origin, validity, created_at, updated_at
                FROM memories
                WHERE account_id = ? AND validity = 1
                ORDER BY created_at DESC
                """,
                (account_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    @staticmethod
    async def correct_memory(account_id: str, memory_id: str, new_content: str) -> bool:
        """Explicitly correct a learned memory."""
        async with get_db() as db:
            await db.execute(
                """
                UPDATE memories
                SET content = ?, origin = 'corrected', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND account_id = ?
                """,
                (new_content, memory_id, account_id),
            )
            await db.commit()
            return True

    @staticmethod
    async def delete_memory(account_id: str, memory_id: str) -> bool:
        """Delete / invalidate a learned memory."""
        async with get_db() as db:
            await db.execute(
                "UPDATE memories SET validity = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND account_id = ?",
                (memory_id, account_id),
            )
            await db.commit()
            return True


extractor = MemoryExtractor()
