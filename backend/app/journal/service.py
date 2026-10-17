"""Action Journal Service - Auditable timeline of all tool calls and outcomes."""

import asyncio
import json
import logging
import uuid
from typing import Any

from app.storage.database import get_db

logger = logging.getLogger(__name__)


class JournalService:
    """Manages the action journal and streams real-time updates to connected clients."""

    def __init__(self):
        # Pub/sub queues for SSE streaming to connected web clients
        self._listeners: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._listeners:
            self._listeners.remove(q)

    async def log_action(
        self,
        account_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        permission_decision: str = "allowed",
        outcome: Any = None,
        error: str | None = None,
        conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an auditable journal entry and broadcast it to the action timeline."""
        entry_id = f"act_{uuid.uuid4().hex[:10]}"
        args_json = json.dumps(arguments)
        outcome_json = json.dumps(outcome) if outcome is not None else None

        entry = {
            "id": entry_id,
            "account_id": account_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "permission_decision": permission_decision,
            "outcome": outcome,
            "error": error,
        }

        # Persist to database
        try:
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO journal_entries
                    (id, account_id, conversation_id, run_id, tool_name, arguments, permission_decision, outcome, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        account_id,
                        conversation_id,
                        run_id,
                        tool_name,
                        args_json,
                        permission_decision,
                        outcome_json,
                        error,
                    ),
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to persist journal entry: {e}")

        # Broadcast to active SSE listeners
        for q in list(self._listeners):
            try:
                await q.put(entry)
            except Exception:
                pass

        return entry

    async def get_history(self, account_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve recent journal entries for this account."""
        async with get_db() as db:
            async with db.execute(
                """
                SELECT id, conversation_id, run_id, tool_name, arguments, permission_decision, outcome, error, timestamp
                FROM journal_entries
                WHERE account_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (account_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    results.append(
                        {
                            "id": r["id"],
                            "conversation_id": r["conversation_id"],
                            "run_id": r["run_id"],
                            "tool_name": r["tool_name"],
                            "arguments": json.loads(r["arguments"]),
                            "permission_decision": r["permission_decision"],
                            "outcome": json.loads(r["outcome"]) if r["outcome"] else None,
                            "error": r["error"],
                            "timestamp": r["timestamp"],
                        }
                    )
                return results


journal = JournalService()
