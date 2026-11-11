"""Development-only synthetic proposal; actual worker/registry/SQLite transaction.

No model selection/provider substitution. Parent SIGKILLs only this owned process.
The pause is a labeled fault-injection seam inside real note SQL, or after commit.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.plugins.registry import registry
from app.runs.service import append_event, claim, submit_run
from app.runs.worker import Worker
from app.storage.database import get_db, init_db


async def main(phase, marker):
    await init_db()
    async with get_db() as db:
        account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
    with Worker() as worker:
        submitted = await submit_run(
            account, "Synthetic note crash proposal", None, "local-ollama", "synthetic-note-crash-request"
        )
        run = await claim(worker.owner)
        assert run["id"] == submitted["id"]
        args = {"action": "create", "title": "Synthetic crash note", "content": "Synthetic crash content"}
        await append_event(run, "tool_call", {"tool": "personal_notes", "arguments": args})

        async def pause():
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"phase": phase, "run_id": run["id"], "account_id": account}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            await asyncio.Event().wait()

        if phase == "before_commit":
            plugin = registry.get("personal_notes")
            original = plugin.in_transaction

            async def before_commit(db, account_id, arguments):
                result = await original(db, account_id, arguments)
                assert result["success"]
                await pause()
                return result

            plugin.in_transaction = before_commit
        result = await registry.dispatch(
            "personal_notes",
            account,
            args,
            operation_id="synthetic-note-crash-operation",
            run_id=run["id"],
            conversation_id=run["conversation_id"],
        )
        assert result["success"]
        await pause()  # Deliberately before durable tool_result delivery.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["before_commit", "after_commit"], required=True)
    parser.add_argument("--marker", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.phase, args.marker))
