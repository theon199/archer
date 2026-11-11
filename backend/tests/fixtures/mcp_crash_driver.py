"""Synthetic tool proposal; actual worker lock, queue, journal, SDK and Docker.

No fake model or inference success: this fixture deliberately skips model selection.
The external development verifier kills this owned process during the real read.
"""

import asyncio

from app.plugins.registry import registry
from app.runs.service import append_event, claim, submit_run
from app.runs.worker import Worker
from app.storage.database import get_db, init_db


async def main():
    await init_db()
    async with get_db() as db:
        account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
    with Worker() as worker:
        submitted = await submit_run(
            account, "Synthetic development crash request", None, "local-ollama", "synthetic-crash-request"
        )
        run = await claim(worker.owner)
        assert run["id"] == submitted["id"]
        await append_event(
            run, "tool_call", {"tool": "mcp_development_container_probe", "arguments": {"mode": "descendant"}}
        )
        await registry.dispatch(
            "mcp_development_container_probe",
            account,
            {"mode": "descendant"},
            operation_id="synthetic-crash-operation",
            run_id=run["id"],
            conversation_id=run["conversation_id"],
        )


if __name__ == "__main__":
    asyncio.run(main())
