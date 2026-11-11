# Note write crash verification

Runtime prerequisite: use the fixed SQLite wrapper for **all** Python/uv application and maintenance commands below. See [SQLite runtime setup](sqlite-runtime.md); define `archer_uv` there and replace `uv` with `archer_uv`. Release users should use the bundled wrapper described in [private deployment](private-deployment.md). Host SQLite 3.45.1 is not the verified runtime.

Run from `backend` with the existing lockfile:

```sh
uv run --locked --extra dev python ../scripts/verify-note-crash.py
```

The verifier creates a private disposable database, synthetic owner/grant and empty MCP configuration. Its explicitly development-only driver skips model selection and calls the real note registry, transaction and worker queue. It pauses either after actual note SQL but before commit, or after atomic commit but before tool-result delivery. The parent sends SIGKILL only to that exact owned driver PID, then starts the real `app.runs.worker` on the same database. It stops only its owned processes and removes temporary data.

`evidence/checkpoint-20-note-crash.json` records **25 passing checks** on 2026-10-24 at 16:28:57 UTC. Before commit, note, revision and operation all roll back to zero; after commit each survives exactly once and the confirmed operation/journal remains succeeded. Both runs retain `needs_reconciliation`, a durable terminal event and a recovery journal, without replay, duplicate generations or an assistant success message. Integrity and foreign-key checks pass after each restart. Aggregate uncertainty means tool-result delivery was interrupted; it does not overwrite an already confirmed note outcome.

Recovery now commits a `run_recovery` journal entry atomically with the terminal run decision/events. This covers pre-commit crashes where the original note transaction and its tool journal both roll back. It records interruption and pending intent without inventing an original tool success or failure. A regression test injects a failure after actual audit insertion and proves run, events and audit all roll back together; subsequent recovery records exactly one terminal outcome/audit.

## Runtime release gap

This DGX Python currently links **SQLite 3.45.1**. [Official SQLite WAL documentation, section 11](https://www.sqlite.org/wal.html) identifies a WAL-reset bug affecting 3.7.0 through 3.51.2, fixed in 3.51.3+ with listed backports 3.44.6 and 3.50.7. Its concurrency conditions are relevant to Archer's multiple WAL connections. A passing bounded process-crash test does not establish absence of that bug. Validate an isolated, reproducible fixed SQLite runtime and rerun durability/deployment checks before release. This checkpoint does not upgrade host Python or alter dependencies.

These checks demonstrate the two selected process-crash boundaries using real local SQL/worker processes and synthetic proposals. They do not exercise model inference, a host power loss, storage failure, GPU termination, every commit boundary or a useful third-party integration. Prior actual model/inference receipts retain their separate scope. Production personal data was not inspected or modified.

The focused `test_sqlite_full_note_write_rolls_back_and_can_be_retried` test also limits the page count of a disposable SQLite connection until an actual `SQLITE_FULL` occurs during a note creation. It verifies transaction rollback, database integrity, and one successful idempotent retry after the limit is removed. This is a simulated database-size limit, not an actual full filesystem, disk hardware failure, power loss, or proof of every write boundary.
