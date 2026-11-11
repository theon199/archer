# Local legacy migration

Runtime prerequisite: use the fixed SQLite wrapper for **all** Python/uv application and maintenance commands below. See [SQLite runtime setup](sqlite-runtime.md); define `archer_uv` there and replace `uv` with `archer_uv`. Release users should use the bundled wrapper described in [private deployment](private-deployment.md). Host SQLite 3.45.1 is not the verified runtime.

The local maintenance CLI provides a read-only review plan and an explicit guarded offline apply. Apply has been verified only on disposable synthetic accounts. No production ownership transfer was performed. Neither operation is exposed as HTTP or an assistant tool.

```sh
cd backend
uv run --locked python -m app.legacy_migration plan \
  --source-account LEGACY_ACCOUNT_ID --target-owner EXISTING_OWNER_ID
```

Use the deployment's ARCHER_DATA_DIR and approved-files configuration. Source must be the explicitly selected demoted scaffold account with password_hash=local_dev_bypass; target must be the sole provisioned Argon2id owner. Nothing chooses or provisions an owner. ready_for_review is a data-check result, not authorization. Plans output counts/blockers/policy/fingerprint, never note/chat content, password hashes or session tokens. Read-only mode=ro/query_only uses a consistent snapshot and closes it; missing databases/directories are not created.

Fingerprints bind schema definitions, recognized migration versions, policy version and both accounts' known scoped rows. Current fingerprints deliberately differ from checkpoint-18 examples. Unknown account_id tables, triggers/views, migration versions, foreign-key issues, wrong account identity, unsettled runs, source/pending MCP resources, uncertain tool outcomes and colliding source/target operation keys block apply. Above 100,000 scoped rows or a row above 1 MiB is refused. These are materialization bounds, not a hard query-time limit or complete hostile-schema analysis. Unknown table contents are not fingerprinted because the plan is blocked.

## Explicit offline apply

Stop only this Archer deployment's API and worker through its normal supervisor; leave Ollama and unrelated services running. The command independently refuses a held worker lock, but cannot detect every separately launched API. --offline acknowledges that operator prerequisite and does not stop processes. Already authorized requests cannot be undone.

Create or reuse a protected backup key outside both data and approved-files roots, then use the fresh reviewed fingerprint/policy and an output path that does not exist:

```sh
uv run --locked python -m app.backup keygen --key-file /PRIVATE/BACKUP/LOCATION/key
uv run --locked python -m app.legacy_migration apply \
  --source-account LEGACY_ACCOUNT_ID --target-owner EXISTING_OWNER_ID \
  --expected-fingerprint REVIEWED_64_CHARACTER_SHA256 \
  --policy-version legacy-transfer-v1 --offline \
  --backup-output /PRIVATE/BACKUP/LOCATION/before-transfer.arbk \
  --backup-key /PRIVATE/BACKUP/LOCATION/key
```

Only the key path is supplied; passwords/key bytes are never arguments. Backup's existing owned regular-file/private-mode checks apply. Keep a separate protected key copy. The command creates a real authenticated encrypted pre-transfer DB/files snapshot, refusing if it fails. It holds the worker lock, checks the plan, creates the backup, takes BEGIN IMMEDIATE and rechecks the snapshot/blockers before changes. Composite foreign keys remain enabled/deferred, are checked before commit, and SQL/audit failure rolls all DB changes back. The backup remains available. On interruption around commit, inspect ownership/journal before retrying: the original fingerprint becomes stale after success. Never regenerate plans and automatically retry.

Transfer preserves identifiers, conversations/messages, runs/events, notes/revisions, terminal operation history, memory/evidence/jobs and journal ownership. Existing owner credentials/content remain intact; the source account stays demoted without a valid password. Both sessions are revoked; source grants/settings discarded; enabled owner grants disabled with revision advancement; learning/retrieval disabled with epoch advancement; queued memory jobs invalidated. Imported memories retain content/evidence but get NULL topic/value and origin legacy, excluding them from personalization. Suppressions are unioned conservatively. File roots/files, credentials and MCP configuration are not reassigned.

Historical tool results and journal payloads stay historical, including original embedded source-account fields. Current row ownership governs authorization. A new durable legacy_migration journal entry/event records source, reviewed fingerprint, policy, backup digest and counts. Original operation keys remain unchanged; collisions refuse. Restart this Archer deployment, sign in again and inspect imports/journal before explicitly granting capabilities or enabling learning. Review legacy file references and connectors separately.

## Reproduce isolated evidence

```sh
uv run --locked --extra dev pytest -q tests/test_legacy_migration.py tests/test_legacy_apply.py
uv run --locked --extra dev python ../scripts/verify-legacy-migration.py
```

Actual plan/apply CLI subprocesses, encrypted backup/authenticated restore and in-process ASGI auth/tools are tested on synthetic data. Checks cover owner password/content preservation, imported note ID/revision/owner, old session rejection, disabled grants, explicit grant before access, memory quarantine, provenance audit and restoration of original ownership. SQL tests cover composite ownership, suppression union, stale fingerprint/live-worker refusal and post-write audit failure rollback. The fault injection is test-only, not a real disk outage. No model inference is claimed.

Evidence: docs/evidence/checkpoint-19-legacy-apply.json and checkpoint-19-migration-summary.json. Checkpoint-18 plan-only receipts describe their earlier source. Power-loss/SIGKILL at commit, live-API misuse, production personal-data migration, file-tree/connector reassignment and off-device retention remain unverified. Restore targets a new directory and quarantines sessions/grants/learning; see [backup-restore.md](backup-restore.md).

Current official [SQLite transactions](https://www.sqlite.org/lang_transaction.html) and [deferred foreign keys](https://www.sqlite.org/pragma.html#pragma_defer_foreign_keys) support the locking/revalidation approach. Existing Python/SQLite and uv/pnpm locks remain unchanged; no model/dependency upgrade is required.
