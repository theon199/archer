"""One private worker process: `uv run --locked python -m app.runs.worker`.

The OS lock is held through execution and recovery, not merely a database lease.
No expired lease authorizes replay while an old process may still be writing.
"""

import asyncio
import fcntl
import json
import time
import uuid

from app.config import settings
from app.memory.extractor import extractor
from app.plugins.mcp_ownership import recover_resources, worker_scope
from app.readiness import validate_runtime
from app.runs.execution import execute
from app.runs.service import append_event, claim, finish, get_run, recover
from app.storage.database import get_db, init_db


class Worker:
    def __init__(self):
        self.owner = uuid.uuid4().hex
        self.lock = None

    def __enter__(self):
        self.lock = open(settings.database_path.with_suffix(".worker.lock"), "a")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError("Another Archer worker holds the database lock") from None
        self.scope_token = worker_scope.set((self.owner, str(settings.database_path.resolve()), self.lock))
        return self

    def __exit__(self, *args):
        worker_scope.reset(self.scope_token)
        if self.lock:
            self.lock.close()
            self.lock = None

    async def recover_resources(self):
        if self.lock is None:
            raise RuntimeError("Worker lock is required for resource recovery")
        return await recover_resources()

    async def heartbeat(self, db=None):
        async def write(connection):
            await connection.execute(
                "INSERT INTO worker_status (id,owner,heartbeat) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,heartbeat=excluded.heartbeat",
                (self.owner, time.time()),
            )

        if db is not None:
            await write(db)
        else:
            async with get_db() as connection:
                await write(connection)
                await connection.commit()

    async def run_one(self):
        if self.lock is None:
            raise RuntimeError("Worker lock is required")
        await self.heartbeat()
        run = await claim(self.owner)
        if run is None:
            return False
        task = asyncio.create_task(self.produce(run))
        started = time.monotonic()
        state, error = "succeeded", None
        try:
            while not task.done():
                row = await get_run(run["account_id"], run["id"])
                if row["status"] == "cancel_requested":
                    state = "canceled"
                    break
                if time.monotonic() - started > settings.run_timeout_seconds:
                    state, error = "failed", "Run time limit exceeded"
                    break
                async with get_db() as db:
                    await self.heartbeat(db)
                    cursor = await db.execute(
                        "UPDATE runs SET lease_until = ? WHERE id = ? AND generation = ? AND lease_owner = ? AND lease_until > ?",
                        (time.time() + 15, run["id"], run["generation"], self.owner, time.time()),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("Worker lease lost")
                    await db.commit()
                await asyncio.wait({task}, timeout=0.1)
            if task.done():
                await task
        except asyncio.CancelledError:
            state, error = "failed", "Worker shutting down; no automatic retry"
            raise
        except Exception:
            # Exception text can contain provider bodies, paths or credentials.
            state, error = "failed", "Execution failed; check local provider availability or tool outcome"
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await finish(run, state, error)
        return True

    async def learn_one(self):
        if self.lock is None:
            raise RuntimeError("Worker lock is required")
        return await extractor.process_one()

    async def produce(self, run):
        pending = ""
        flushed_at = time.monotonic()
        events = 0
        try:
            async for item in execute(run):
                events += 1
                if events > 4096:
                    raise RuntimeError("Event limit exceeded")
                data = json.loads(item["data"])
                if item["event"] == "token":
                    pending += data["token"]
                    if len(pending) < 512 and time.monotonic() - flushed_at < 0.05:
                        continue
                if pending:
                    batch = pending
                    pending = ""
                    await append_event(run, "token", {"token": batch})
                    flushed_at = time.monotonic()
                if item["event"] != "token":
                    await append_event(run, item["event"], data)
        except Exception:
            # Preserve a short buffered prefix on provider failure while the
            # normal lease and cancellation checks still permit writes.
            if pending:
                await append_event(run, "token", {"token": pending})
            raise
        if pending:
            await append_event(run, "token", {"token": pending})


async def main():
    validate_runtime()
    await init_db()
    from app.plugins.registry import sync_owner_default_grants

    await sync_owner_default_grants()
    with Worker() as worker:
        await recover()
        await worker.recover_resources()
        while True:
            if not await worker.run_one():
                if not await worker.learn_one():
                    await asyncio.sleep(0.25)


if __name__ == "__main__":
    asyncio.run(main())
