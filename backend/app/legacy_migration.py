"""Local legacy review and guarded offline transfer; never an HTTP/tool action."""

import argparse
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

TABLES = {
    "sessions",
    "conversations",
    "messages",
    "runs",
    "run_events",
    "notes",
    "tool_grants",
    "tool_operations",
    "mcp_resources",
    "note_revisions",
    "memories",
    "memory_settings",
    "memory_jobs",
    "memory_evidence",
    "memory_suppressions",
    "journal_entries",
    "journal_events",
}
POLICY_VERSION = "legacy-transfer-v1"
MAX_ROWS = 100000
MAX_ROW_BYTES = 1048576


def inspect_plan(database: Path, source: str, target: str) -> dict:
    """Inspect one consistent SQLite snapshot; return counts/hash, never content.

    The fingerprint is an evidence identifier for a guarded apply, not
    consent or a credential. Applying requires separate explicit offline arguments.
    """
    if source == target or not all(1 <= len(value) <= 128 for value in (source, target)):
        raise ValueError("Distinct source and target IDs of 1–128 characters are required")
    database = database.resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        schema = db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        ).fetchall()
        scoped = set()
        for _, name, _ in schema:
            if name.startswith("sqlite_") or name not in {row[1] for row in schema if row[0] == "table"}:
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            if any(row[1] == "account_id" for row in db.execute(f"PRAGMA table_info({quoted})")):
                scoped.add(name)
        blockers = []
        migrations = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        if set(migrations) != {"003_tool_permissions_note_revisions", "004_memory_evidence_epochs"}:
            blockers.append("migration_version_requires_review")
        if any(row[0] in {"trigger", "view"} for row in schema):
            blockers.append("trigger_or_view_requires_review")
        if scoped != TABLES:
            blockers.append("account_scoped_schema_requires_review")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            blockers.append("existing_foreign_key_violation")
        owners = db.execute("SELECT id FROM accounts WHERE is_owner=1").fetchall()
        old = db.execute("SELECT password_hash,is_owner FROM accounts WHERE id=?", (source,)).fetchone()
        owner = db.execute("SELECT password_hash FROM accounts WHERE id=?", (target,)).fetchone()
        if not old or old[0] != "local_dev_bypass" or old[1]:
            blockers.append("source_must_be_demoted_legacy_bypass_account")
        if owners != [(target,)] or not owner or not owner[0].startswith("$argon2id$"):
            blockers.append("target_must_be_the_only_secure_provisioned_owner")
        if db.execute(
            "SELECT 1 FROM runs WHERE status IN ('queued','generating','cancel_requested') LIMIT 1"
        ).fetchone():
            blockers.append("unsettled_runs_require_review")
        if db.execute("SELECT 1 FROM mcp_resources WHERE account_id=? LIMIT 1", (source,)).fetchone():
            blockers.append("legacy_mcp_resource_ownership_requires_review")
        if db.execute("SELECT 1 FROM mcp_resources WHERE status='pending' LIMIT 1").fetchone():
            blockers.append("pending_mcp_resources_require_review")
        if db.execute(
            "SELECT 1 FROM tool_operations WHERE account_id IN (?,?) AND status IN ('running','needs_reconciliation') LIMIT 1",
            (source, target),
        ).fetchone():
            blockers.append("unsettled_tool_outcomes_require_review")
        for table, column, code in [
            ("runs", "client_operation_id", "run_operation_key_collision"),
            ("tool_operations", "operation_id", "tool_operation_key_collision"),
        ]:
            if db.execute(
                f"SELECT 1 FROM {table} a JOIN {table} b ON a.{column}=b.{column} WHERE a.account_id=? AND b.account_id=? LIMIT 1",
                (source, target),
            ).fetchone():
                blockers.append(code)
        counts = {}
        total = 0
        digest = hashlib.sha256(json.dumps(schema, sort_keys=True).encode())
        digest.update(json.dumps([POLICY_VERSION, migrations, source, target]).encode())
        for table in sorted(scoped & TABLES):
            source_count = db.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (source,)).fetchone()[0]
            target_count = db.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (target,)).fetchone()[0]
            counts[table] = {"source": source_count, "target": target_count}
            total += source_count + target_count
        if total > MAX_ROWS:
            raise ValueError("Plan exceeds bounded row budget; no data changed")
        for table in ["accounts", *sorted(scoped & TABLES)]:
            key = "id" if table == "accounts" else "account_id"
            # Bound each SQL row before materializing content in Python.
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            lengths = "+".join(
                'COALESCE(length(CAST("' + column.replace('"', '""') + '" AS BLOB)),0)' for column in columns
            )
            if db.execute(
                f"SELECT 1 FROM {table} WHERE {key} IN (?,?) AND ({lengths})>? LIMIT 1", (source, target, MAX_ROW_BYTES)
            ).fetchone():
                raise ValueError("Plan exceeds bounded row-size budget; no data changed")
            digest.update(table.encode())
            for row in db.execute(f"SELECT * FROM {table} WHERE {key} IN (?,?) ORDER BY rowid", (source, target)):
                canonical = [{"blob": value.hex()} if isinstance(value, bytes) else value for value in row]
                digest.update(json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")
        return {
            "mode": "read_only_plan",
            "source_account_id": source,
            "target_owner_id": target,
            "fingerprint_sha256": digest.hexdigest(),
            "counts": counts,
            "blockers": blockers,
            "ready_for_review": not blockers,
            "apply_supported": True,
            "policy_version": POLICY_VERSION,
            "proposed_policy": {
                "content": "Preserve identifiers, source provenance, note revisions, run/events and historical outcomes; transfer only through an atomic guarded apply.",
                "authority": "Revoke both accounts sessions; do not import grants, credentials, file roots or MCP mappings; disable owner grants and learning/retrieval for review.",
                "memory": "Preserve legacy content/evidence as untrusted review data, never silently enable it for personalization; retain deletion suppression.",
                "prerequisites": "Encrypted backup; stop only this deployment API/worker; worker lock; recheck fingerprint, schema, collisions and foreign keys in apply transaction.",
            },
        }


def _record_transfer(db, target, source, fingerprint, backup_digest, counts):
    entry = "act_" + uuid.uuid4().hex
    arguments = json.dumps(
        {
            "action": "legacy_transfer",
            "source_account_id": source,
            "fingerprint_sha256": fingerprint,
            "policy_version": POLICY_VERSION,
            "backup_sha256": backup_digest,
        }
    )
    outcome = json.dumps(
        {
            "success": True,
            "status": "transferred",
            "source_counts": {table: value["source"] for table, value in counts.items()},
            "credentials_transferred": False,
            "files_transferred": False,
            "legacy_memories_quarantined": True,
        }
    )
    db.execute(
        "INSERT INTO journal_entries(id,account_id,tool_name,arguments,permission_decision,outcome) VALUES (?,?,'legacy_migration',?,'local_filesystem',?)",
        (entry, target, arguments, outcome),
    )
    cursor = db.execute("SELECT * FROM journal_entries WHERE id=?", (entry,))
    row = dict(zip([column[0] for column in cursor.description], cursor.fetchone(), strict=True))
    row["arguments"], row["outcome"] = json.loads(arguments), json.loads(outcome)
    db.execute(
        "INSERT INTO journal_events(account_id,journal_id,data) VALUES (?,?,?)", (target, entry, json.dumps(row))
    )


def apply_plan(database, source, target, fingerprint, policy_version, backup_output, backup_key, *, offline=False):
    """Local explicit offline operation; tests use only disposable synthetic data."""
    from app.backup import backup
    from app.config import settings
    from app.runs.worker import Worker

    if not offline or policy_version != POLICY_VERSION or len(fingerprint) != 64:
        raise ValueError("Offline acknowledgment, current policy and exact fingerprint required")
    if Path(database).is_symlink():
        raise ValueError("Migration database must not be a symlink")
    database = Path(database).resolve(strict=True)
    if database != (settings.data_dir / "archer.db").resolve() or database.is_symlink():
        raise ValueError("Migration must use the configured existing database")
    with Worker():
        reviewed = inspect_plan(database, source, target)
        if reviewed["blockers"] or reviewed["fingerprint_sha256"] != fingerprint:
            raise ValueError("Blocked or stale migration plan; no ownership changed")
        snapshot = backup(settings.data_dir, settings.approved_files_root, Path(backup_output), Path(backup_key))
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                # This second read connection sees the exact committed snapshot;
                # BEGIN IMMEDIATE prevents concurrent writers until our commit.
                current = inspect_plan(database, source, target)
                if current["blockers"] or current["fingerprint_sha256"] != fingerprint:
                    raise ValueError("Migration plan changed while preparing backup")
                db.execute("PRAGMA defer_foreign_keys=ON")
                if db.execute("PRAGMA defer_foreign_keys").fetchone()[0] != 1:
                    raise ValueError("Deferred foreign-key validation unavailable")
                db.execute("DELETE FROM sessions WHERE account_id IN (?,?)", (source, target))
                db.execute("DELETE FROM tool_grants WHERE account_id=?", (source,))
                db.execute(
                    "UPDATE tool_grants SET enabled=0,revision=revision+1 WHERE account_id=? AND enabled=1", (target,)
                )
                db.execute(
                    "UPDATE memory_jobs SET status='invalidated' WHERE account_id IN (?,?) AND status='queued'",
                    (source, target),
                )
                db.execute("DELETE FROM memory_settings WHERE account_id=?", (source,))
                db.execute(
                    "INSERT INTO memory_settings(account_id,learning_enabled,retrieval_enabled,epoch) VALUES (?,0,0,1) ON CONFLICT(account_id) DO UPDATE SET learning_enabled=0,retrieval_enabled=0,epoch=epoch+1",
                    (target,),
                )
                db.execute(
                    "UPDATE memories SET subject_key=NULL,value=NULL,origin='legacy' WHERE account_id=?", (source,)
                )
                db.execute(
                    "INSERT OR IGNORE INTO memory_suppressions(account_id,subject_key,source_digest,created_at) SELECT ?,subject_key,source_digest,created_at FROM memory_suppressions WHERE account_id=?",
                    (target, source),
                )
                db.execute("DELETE FROM memory_suppressions WHERE account_id=?", (source,))
                for table in sorted(
                    TABLES - {"sessions", "tool_grants", "memory_settings", "memory_suppressions", "mcp_resources"}
                ):
                    db.execute(f"UPDATE {table} SET account_id=? WHERE account_id=?", (target, source))
                _record_transfer(db, target, source, fingerprint, snapshot["sha256"], current["counts"])
                if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Transfer failed foreign-key validation")
                db.commit()
            except BaseException:
                db.rollback()
                raise
    return {
        "success": True,
        "status": "transferred",
        "policy_version": POLICY_VERSION,
        "source_account_id": source,
        "target_owner_id": target,
        "reviewed_fingerprint_sha256": fingerprint,
        "backup_sha256": snapshot["sha256"],
        "legacy_memories_quarantined": True,
    }


def main():
    from app.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "apply"])
    parser.add_argument("--source-account", required=True)
    parser.add_argument("--target-owner", required=True)
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--policy-version")
    parser.add_argument("--backup-output", type=Path)
    parser.add_argument("--backup-key", type=Path)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="This Archer API/worker is stopped; command independently checks worker lock",
    )
    args = parser.parse_args()
    if args.command == "apply" and not all(
        [args.expected_fingerprint, args.policy_version, args.backup_output, args.backup_key, args.offline]
    ):
        parser.error("apply requires fingerprint, policy version, backup output/key and --offline")
    try:
        # Avoid database_path property: planning must not create missing data dirs.
        if args.command == "apply":
            result = apply_plan(
                settings.data_dir / "archer.db",
                args.source_account,
                args.target_owner,
                args.expected_fingerprint,
                args.policy_version,
                args.backup_output,
                args.backup_key,
                offline=args.offline,
            )
            print(json.dumps(result, indent=2))
            return 0
        plan = inspect_plan(settings.data_dir / "archer.db", args.source_account, args.target_owner)
    except (ValueError, OSError, sqlite3.Error, RuntimeError):
        raise SystemExit("Legacy maintenance failed; inspect local backup/state before retrying") from None
    print(json.dumps(plan, indent=2))
    return 0 if plan["ready_for_review"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
