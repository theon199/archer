# Private backup and clean restore

**Current V1 choice:** retain encrypted backups on this system for seven days. This does not protect against loss of the machine or its storage. The owner selected a fresh V1 database at `/home/sri/.local/share/archer-v1/data`; the prior database in `/home/sri/archer/data` is untouched. After owner provisioning a real backup was restored to a temporary location; the restored database contained one secure owner and zero sessions. The user-level `archer-v1-backup.timer` runs rotation daily, using the units in `config/systemd/`, and was manually checked to succeed. Its source tree, virtual environment, fixed SQLite runtime and paths must remain available. Check `systemctl --user status archer-v1-backup.timer archer-v1-backup.service` for failures and repeat restore drills after adding data.

For a current-schema database, create a private dedicated backup directory and keep the key outside it and outside the source roots. Run once per desired backup interval under the fixed SQLite runtime:

```sh
uv run --locked python -m app.backup_rotation \
  --data-dir /path/to/archer-data --files-root /path/to/approved-files \
  --directory /path/to/private-managed-backups --key-file /path/to/private-backup.key
```

Rotation creates a new encrypted archive and verifies it with a temporary restore before removing eligible managed archives older than seven days. It only removes owned, single-link regular files matching its exact managed naming pattern in that dedicated directory; symlinks and unrelated names are left alone. A failed backup or restore check does not prune old files. The restore check temporarily writes decrypted data inside the private backup directory and removes the temporary copy when finished; abrupt process or host failure can leave temporary files behind. No scheduler is installed by this command. Never use the managed directory for unrelated files with the same naming pattern.

Runtime prerequisite: use the fixed SQLite wrapper for **all** Python/uv application and maintenance commands below. See [SQLite runtime setup](sqlite-runtime.md); define `archer_uv` there and replace `uv` with `archer_uv`. Release users should use the bundled wrapper described in [private deployment](private-deployment.md). Host SQLite 3.45.1 is not the verified runtime.

This runbook covers Archer's current local data format, not a complete production
deployment. It was exercised on this ARM64 DGX with Python 3.12, SQLite 3.45.1 and
cryptography 50.0.2. Use the existing `backend/uv.lock` and the same reviewed source
revision. The CLI never uploads data, starts/stops a service, or changes networking.
A separate-device copy and full deployment/cold-start verification are still open.

## What is captured

`python -m app.backup create` uses SQLite's online backup API, including committed
WAL pages. It does not copy the live database file or its WAL independently.
The encrypted payload contains the consistent database, approved files, and a
manifest of SHA-256 hashes, sizes, schema migrations and note revisions.
Accounts, password hashes, grants, chat, events, operation receipts, notes/history,
preference evidence and suppression records live in that database.

The archive does **not** contain `.env`, connector credentials, backup keys, model
weights, drivers, application source or dependency caches. Preserve the reviewed
source and lockfiles separately, and recover secrets through their separate owner
storage. Optional missing search credentials must not be replaced with fake results.

SQLite writers may continue during capture. Approved files are read-only from
Archer; pause external editing/imports for the whole backup. The CLI rejects
observed file/directory mutations but is not a filesystem-wide snapshot and cannot
guarantee a single instant across independently changing files. The manifest
verifies the exact copied bytes and the database's note revisions.

## Key and backup

From `backend/`, sync the locked dependencies:

```bash
uv sync --locked
```

Choose private, existing storage for backups and separate storage for the key.
The following paths are examples to replace; these commands were not run against
production owner data during the verification drill.

```bash
ARCHER_SOURCE_DATA="/path/to/archer-data"
ARCHER_FILES_ROOT="$ARCHER_SOURCE_DATA/approved_files"
ARCHER_BACKUP_DIR="/path/to/private-backup-volume/archer"
ARCHER_KEY_DIR="/path/to/separate-private-key-storage"
mkdir -p -m 700 "$ARCHER_BACKUP_DIR" "$ARCHER_KEY_DIR"
ARCHER_KEY_FILE="$ARCHER_KEY_DIR/archer-backup.key"
uv run --locked python -m app.backup keygen --key-file "$ARCHER_KEY_FILE"
```

Generate the key once. It is a random 32-byte AES-256 key, owned by the current
user and mode 0600. Never print it, put it in chat/source/environment examples,
or keep its only copy with the archive. Existing keys are never overwritten.
A lost key makes the encrypted backups unrecoverable.

```bash
ARCHER_BACKUP_FILE="$ARCHER_BACKUP_DIR/archer-$(date -u +%Y%m%dT%H%M%SZ).arbk"
uv run --locked python -m app.backup create \
  --data-dir "$ARCHER_SOURCE_DATA" \
  --files-root "$ARCHER_FILES_ROOT" \
  --output "$ARCHER_BACKUP_FILE" \
  --key-file "$ARCHER_KEY_FILE"
```

The result reports format, file count, encrypted size and SHA-256. Keep that
receipt with operational records. Output is mode 0600, authenticated with
AES-256-GCM, and published atomically without overwriting another file. The random
96-bit nonce and format header are authenticated. Authentication is verified before
restore inspects any archive member.

The owner chose local retention; an off-device copy is not required for V1. An
archive and key on the same DGX do not protect against loss of that system.

## Clean restore

Use the exact compatible source/lockfiles. Choose a **nonexistent** destination
inside a private existing parent on local storage. Never overwrite the original
runtime directory. Stop only the Archer instance that will use this restored
location before switching its configuration; do not stop unrelated services.

```bash
ARCHER_RESTORED_DATA="/path/to/private-parent/archer-restored"
uv run --locked python -m app.backup restore \
  --input "$ARCHER_BACKUP_FILE" \
  --destination "$ARCHER_RESTORED_DATA" \
  --key-file "$ARCHER_KEY_FILE"
```

Restore verifies authentication, safe regular archive members, the full manifest,
SQLite integrity/foreign keys and note revisions. Absolute paths, traversal,
links, duplicate/unknown members and mismatched hashes are rejected. Extraction
uses a private staging directory; Linux `renameat2(RENAME_NOREPLACE)` publishes the
verified result atomically. Even an existing empty destination is refused.
There is no unsafe rename fallback on an unsupported platform.

Before publication it also:

- Revokes all sessions and clears old login throttling; fresh owner login is required.
- Preserves grant records but disables them and advances revisions. The owner must
  review and explicitly renew needed tool actions through the permissions UI/API.
- Quarantines every queued/active/cancel-requested run as `needs_reconciliation`.
  An older snapshot may predate effects completed later; queued jobs are not safe
  to replay automatically. Persisted state/done events explain the quarantine.
- Marks unfinished tool operations and their journal receipts for reconciliation.
- Invalidates queued learning, advances epochs, and disables both learning and
  preference retrieval until the owner reviews the restored snapshot.
- Preserves deletion suppressions already included in the snapshot.

Read `RESTORE-REVIEW.json`. Inspect the action journal and actual external effects
before creating replacement work. Do not manually reset uncertain operations to
queued. Source/snapshot grant revocations or preference deletions that happened
**after** the snapshot cannot be reconstructed from that older archive: review
retention dates, reapply deletions/revocations, and leave learning/retrieval off
until that review is complete. Old encrypted backups retain old data; choose a
retention policy and expire obsolete archives/key copies deliberately. This CLI
does not silently delete any backup, source data or key.

After review, run only the authenticated private application, overriding both
paths so approved files follow the restored data:

```bash
ARCHER_DATA_DIR="$ARCHER_RESTORED_DATA" \
ARCHER_APPROVED_FILES_ROOT="$ARCHER_RESTORED_DATA/approved_files" \
  uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In another terminal use the same path overrides with
`uv run --locked python -m app.runs.worker`. Renew grants explicitly after login.
The restore does not create an owner password, enable cloud inference, launch a
model, expose a public port or turn routine suggestions into scheduled work.
Production supervision, complete immutable deployment manifests and browser
verification remain separate release requirements.

## Bounds and failure handling

Empty approved subdirectories are not archived; recreate any needed empty account
roots manually. The current bounded format permits at most 10,000 data files including the DB,
256 MiB total plaintext file contents, a 1 MiB manifest, and a 512 MiB tar archive.
It is intentionally uncompressed and uses bounded in-memory authenticated
encryption; allow several times that size in RAM and staging disk. Over-limit
inputs fail before a final artifact is published; there is no silent omission.
Approved directories must contain regular files/directories, with no symlinks or
special files. Long supported file paths use PAX archive metadata.

Keys and archive outputs must be outside both source data and approved-file
roots. Backup/restore require a writable private destination parent, not root
privileges. A crash may leave a private `.archer-backup-*` or `.archer-restore-*`
staging directory; inspect and remove only the abandoned directory belonging to
that interrupted operation. Plaintext staging is private but is not secure-erased;
use appropriately protected local storage. CLI errors print an exception class,
not personal paths, payload contents or key material.

## Repeat the verified drill

This creates only synthetic records in a temporary directory and deletes that
temporary fixture afterward. It invokes the actual CLI in separate processes,
checks a committed WAL revision, restores to a clean directory, rejects an old
session, signs in anew, requires owner grant renewal, and confirms the restored
worker has no automatically executable unfinished job. No inference/provider is
mocked or invoked.

```bash
uv run --locked --extra dev pytest tests/test_backup.py -q
uv run --locked --extra dev python ../scripts/verify-backup-restore.py
```

Optionally add `--evidence /path/to/drill-result.json` to retain its non-secret
receipt. Current recorded evidence is
[`checkpoint-5-backup-restore.json`](evidence/checkpoint-5-backup-restore.json).
The drill is local and synthetic; it does not establish a production-data restore,
off-device redundancy, encrypted-disk protection or full deployment readiness.

MCP resource ownership records in a snapshot are restored as `restored_unverified`, never used for automatic Docker cleanup. The source deployment may still own those named containers. Review source/target identities separately; do not change restored records to pending as a shortcut. New ordinary worker restarts reconcile only that live database's pending resources after matching recorded identity labels/image and use the immutable inspected container ID. Earlier snapshots without the resource table remain supported.
