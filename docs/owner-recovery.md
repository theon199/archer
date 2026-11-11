# Local owner credential recovery

Runtime prerequisite: use the fixed SQLite wrapper for **all** Python/uv application and maintenance commands below. See [SQLite runtime setup](sqlite-runtime.md); define `archer_uv` there and replace `uv` with `archer_uv`. Release users should use the bundled wrapper described in [private deployment](private-deployment.md). Host SQLite 3.45.1 is not the verified runtime.

Recovery requires trusted local filesystem access to the Archer data directory. There is no HTTP recovery endpoint, email dependency, default password or remote reset token. Local filesystem administrators already control the database and secrets. Do not expose the recovery command as an assistant tool.

1. Back up the current database and approved files using docs/backup-restore.md. Retain the encryption key separately.
2. Stop only this Archer deployment's API and worker through its normal service supervisor. Do not stop Ollama or unrelated services. The command independently refuses a held worker lock; it cannot detect every independently launched API, so stopping the API is an operator prerequisite.
3. Inspect the owner ID locally without reading password hashes:

```sh
cd backend
uv run --locked python -c 'import sqlite3; from app.config import settings; db=sqlite3.connect(settings.database_path); print(db.execute("SELECT id, username FROM accounts WHERE is_owner=1").fetchall())'
uv run --locked python -m app.owner_recovery --account-id EXISTING_OWNER_ID
```

Use the same ARCHER_DATA_DIR and runtime configuration as the deployment. The command does not initialize a missing database, choose an account, provision another owner or claim legacy data. It requires interactive stdin and aborts if echo-free password input is unavailable. It prompts twice for a 12–1024 character password; do not put passwords in shell arguments, environment variables, chat or logs. It requires exactly one owner matching the supplied ID. A missing/incorrect/ambiguous owner fails closed.

Unsettled queued/generating/cancel-requested runs and pending MCP resources cause refusal. Inspect/cancel/reconcile these through the existing deployment's normal recovery procedure before retrying. Do not edit uncertain write outcomes into success or rerun them to clear the check.

The reset holds the existing worker filesystem lock and commits one SQLite transaction: new Argon2id password hash; revocation of this owner's sessions; disabling active tool grants with revision increments; disabling learning/retrieval and advancing its epoch; canceling queued memory jobs; clearing the service's global login-attempt throttle; and a sanitized durable owner_recovery journal entry/event. The owner ID, username, conversations, messages, runs/events, notes/revisions, memories/evidence and other accounts remain unchanged. Re-enabling grants or memory is a separate explicit review after signing in.

Restart this Archer deployment, sign in with the new password, and inspect the recovery journal. Old sessions/passwords must fail. Login also rechecks the password hash inside the session-insert transaction, preventing verification started before a reset from minting a new session afterward. Requests already authorized before revocation cannot be undone; the offline operator prerequisite prevents those application writes during recovery. Recovery does not prove a compromised host is safe or rotate separately configured provider keys.

Tests use isolated synthetic owners, actual Argon2 and SQLite/HTTP authentication. A deterministic synchronization hook pauses verification to exercise the stale-login race; a synthetic journal failure proves transaction rollback. These are labeled unit fault injections, not a real storage outage or third-party integration. The actual terminal drill below verifies the CLI separately from these unit injections. Legacy bypass-account data remains under its original account; guarded migration is separate from recovery; see legacy-migration.md for its synthetic validation and limits.

Existing locked Argon2 choices are retained. Current official [argon2-cffi API guidance](https://argon2-cffi.readthedocs.io/en/stable/api.html) confirms PasswordHasher uses Argon2id with random salts and supports verification of encoded hashes. No new dependencies or model changes are required.


## Reproduce the terminal drill

On this Linux DGX, with backend locked dependencies installed:

```sh
cd backend
uv run --locked --extra dev python ../scripts/verify-owner-recovery.py
```

This verifier starts the real `app.owner_recovery` module in a controlling pseudo-terminal, inspects its ECHO flag at each prompt, submits generated disposable passwords only through the terminal, and checks exit codes and durable SQLite effects. Mismatch, Ctrl-C, terminal EOF, wrong/ambiguous owner, live worker and pending MCP resource cases must leave the logical database unchanged. The MCP resource is an explicitly synthetic unlaunched database record; no Docker/connector invocation is claimed. Noninteractive input is separately refused. Success verifies old-password/session rejection, new-password login, preserved note/owner ID, disabled grants and learning, and one sanitized journal event. Authentication checks exercise the actual ASGI app in-process; no live HTTP server is started.

Receipt: `docs/evidence/checkpoint-13-owner-recovery-pty.json`; source/lock/runtime hashes: `checkpoint-13-recovery-summary.json`. Terminal transcripts, generated passwords and session tokens stay in memory and are never written to receipts or logs. Every owned child is reaped; timeouts kill only that exact verifier child. A private temporary directory and its synthetic data are removed afterward. The receipt starts with passed:false/in_progress and exceptions overwrite it with sanitized failure metadata so an old passing receipt cannot masquerade as a fresh run. Production data and existing services are untouched.

Python [pty](https://docs.python.org/3.12/library/pty.html) supplies the child controlling terminal; [getpass](https://docs.python.org/3.12/library/getpass.html) documents its warning when echo-free input is unavailable. Recovery rejects that warning rather than accepting echoed passwords. The actual drill proves normal echo suppression and cancellation; an OS-level terminal ioctl failure has not been induced. Production legacy migration, live-API misuse during offline recovery and host/storage failure remain separate unverified cases.

A separate read-only legacy migration planner is now available: see [legacy-migration.md](legacy-migration.md). It produces counts, blockers and a content-sensitive snapshot fingerprint without moving records. A separate explicit offline apply now requires a fresh policy-bound fingerprint and creates an encrypted backup. Password recovery does not imply ownership migration; production transfer has not been performed.
