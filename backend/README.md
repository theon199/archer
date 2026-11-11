# Archer Core Backend

FastAPI service powering Archer V1 on NVIDIA DGX Spark.

## Quickstart

```bash
# Sync dependencies and create venv
uv sync --all-extras

# Run development server
uv run uvicorn app.main:app --reload --port 8000

# Run test suite
uv run pytest
```

## Owner authentication (local checkpoint)

Use `uv sync --locked --all-extras`, then `uv run --locked python -m app.auth`
to provision the single owner interactively. The password is hashed with Argon2id;
no password belongs in command-line arguments, environment examples, or source.
The browser sign-in form uses `/api/auth/login`; tokens remain in component memory
and expire after 12 hours. Reloading requires sign-in. Sign out revokes that token.
API clients use `Authorization: Bearer <token>`; account-ID headers and cookies
are not credentials. Session tokens are stored only as SHA-256 digests.

Only `/api/health` and service documentation/root metadata are public. All data,
model status, tools and journal routes require a session. Ten login attempts per
five minutes are admitted across this single-owner installation, stored in SQLite;
this intentionally favors bounded password hashing over public availability.
Keep services on loopback; the dev defaults bind `127.0.0.1`. Public deployment is
not validated or enabled. CORS allows only the two loopback Vite origins, without
cookies. Do not put tokens in URLs or persistent browser storage.

The scaffold's `local_dev_bypass` password is never accepted. Provisioning disables
its owner flag and revokes its sessions while preserving its records. Use a new
username if that legacy row already uses yours. Legacy records are not silently
transferred to the new owner: reviewed ownership migration/recovery is still
required. No runtime owner password has been chosen by the coding agent.

Missing Tavily credentials return `unavailable`, not fabricated results. Cloud
inference is rejected even if old cloud keys are present. Real local inference,
durable runs and replay, full tool grants, preference controls, recovery and
browser end-to-end checks remain outstanding; see `docs/overnight-handoff.md`.

## Durable conversations and foreground worker

Run the API and the **separate** worker against the same `ARCHER_DATA_DIR`:

```bash
# Terminal 1, backend/
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000
# Terminal 2, backend/
uv run --locked python -m app.runs.worker
# Terminal 3, frontend/
pnpm dev --host 127.0.0.1
```

`make dev` starts all three; `make dev-worker` starts only the worker. The worker
has no listener. Another worker on this database fails its OS file lock. Gateway
restarts and browser disconnects do not own execution. Use a local filesystem,
not shared hosts or network filesystems.

Authenticated API (bearer token required):

- `POST /api/v1/runs`: JSON `message`, `client_operation_id`, optional
  `conversation_id` and local `provider`. HTTP 202 returns the persisted queued
  run. Retry lost responses with the same operation ID and exact input. Reusing
  an ID with changed input returns 409.
- `POST /api/v1/conversations/{id}/runs`: conversation-scoped submission.
- `GET /api/v1/runs/{id}`: durable state, complete output so far, safe error.
- `GET /api/v1/runs/{id}/events`: SSE with increasing integer sequence IDs.
  `Last-Event-ID` (or `?after=N`) resumes after that cursor. Ownership is checked;
  connected streams recheck expiration and revocation.
- `POST /api/v1/runs/{id}/cancel`: idempotent queued/active cancellation.
- `GET /api/v1/conversations` and `GET /api/v1/conversations/{id}/messages`:
  owned saved history (current list/message limits 100/500).

Legacy `POST /api/chat/stream` now returns submission JSON rather than executing
inside SSE. Migrate callers to the API above; supply `client_operation_id` for
idempotent compatibility submissions. The browser uses the new API, retains
conversation identity within its session, performs bounded replay/reconnect and
exposes cancellation. A saved-conversation picker and reload-resume browser flow
remain pending; durable history is currently accessible through the API.

Each run row is also its foreground job. The worker claims FIFO jobs atomically,
increments a fencing generation and renews a 15-second lease while holding its
process-lifetime lock. Limits: 20 queued/active jobs globally, 180-second execution
deadline (`ARCHER_RUN_TIMEOUT_SECONDS`), 8 tool calls, 4096 generated events,
128000 output characters, and 1 MiB per persisted event. Text deltas are batched
at 512 characters or 50 ms at the next incoming token. Events are retained with
the run indefinitely; no expiration/compaction is implemented yet. SSE never
holds a database transaction open for its lifetime.

After process loss, the replacement acquires the OS lock first, preserves queued
jobs and fails interrupted active jobs. Outstanding tool intent becomes
`needs_reconciliation`; interrupted operations are never automatically retried.
Inspect the journal and actual effects before any manual recovery. Successful
assistant-message persistence and terminal events commit together. Partial
output remains in snapshots. With no worker, the API accepts work that stays
queued; worker-readiness health and production supervision remain outstanding.

`make test` uses isolated temporary SQLite databases and explicitly mocked
inference for backend execution tests. Frontend Node tests verify fragmented
Unicode SSE, duplicate IDs and reader cancellation. These are not real-model or
browser end-to-end evidence. Full grants, atomic note-operation journaling,
background preference learning, deployment and backup/restore remain open.

## Tool permissions and note revisions

The owner's selected built-in actions and configured time connector are enabled
by default on initial setup; explicit revocations remain in force. No grant is inferred from a user preference, model output, note/file
content or MCP description. `GET /api/permissions` lists capabilities and grants;
`PUT /api/permissions/{tool_name}/{action}` accepts `{"enabled":true}` or
`{"enabled":false}`. These routes require the authenticated owner and are not
registered tools. Grants bind the current builtin tool version (`1`); bumping a
tool version requires renewed grants. Note UI provides explicit allow/revoke
controls; file/search grants currently use the authenticated API.

Supported capabilities are notes `list/get/history/create/update/delete`, files
`list/read`, and web search `search`. Files only read
`ARCHER_APPROVED_FILES_ROOT/<account_id>/`; create/populate that directory manually
with approved files. Existing shared-root files are not automatically migrated.
Symlinks (including the account directory), traversal, absolute paths and special
files are denied. Reads are bounded to 1 MiB and listings to 200 entries. A search
grant permits query egress to Tavily; missing credentials return unavailable.
No arbitrary URL retrieval is implemented. MCP is currently unconfigured and
unregistered; unknown MCP calls/grants are denied, not simulated.

Direct note routes and assistant calls use the same registry policy. `POST
/api/notes`, `PUT /api/notes/{id}` and `DELETE /api/notes/{id}` require a unique
`Idempotency-Key` header. Retries reuse the same key and exact normalized arguments;
changed input returns 409. Update JSON requires `expected_revision`; delete uses
`?expected_revision=N`. `GET /api/notes/{id}` returns content and current revision;
`GET /api/notes/{id}/history` requires its own history grant. Foreign/missing notes
return 404. Stale revisions return 409. Deletion hides the current note while
retaining revision history; permanent retention/deletion policy remains pending.

The additive `003_tool_permissions_note_revisions` migration preserves existing
notes at revision 1 and records their initial history. Mutations, history,
idempotent operation result, permission receipt and redacted journal event commit
in one SQLite transaction. A journal failure rolls back the change. Replays return
the recorded result without another mutation, after checking the grant is still
valid. Read-only file/search operations record intent before I/O and result after;
interrupted reads are not silently retried. No external-write plugin is enabled.

Journal permission decisions distinguish `allowed`/`denied` from execution outcome:
an allowed but unavailable search stays unavailable. Bodies, titles, paths and
queries are redacted in journal arguments; note IDs/revisions and safe status
remain inspectable. Full note/operation results stay in owned persistent storage.
`GET /api/journal/stream` now replays persisted transitions across processes using
`Last-Event-ID` or `?after=N`, with ownership and session-revocation checks. New
transition events are retained indefinitely; pre-migration entries remain available
in journal history but are not backfilled into the transition stream.

## Preference evidence and learning controls

The existing worker performs a bounded local learning pass only when there are
no queued/active foreground runs. Successful runs enqueue recognized user messages
in the same transaction as completion; at most 100 learning jobs wait globally.
This is application work inside the existing worker, not a new scheduler.

Learning currently recognizes **entire messages** in a small non-sensitive grammar:

- `I prefer concise answers.` / `I prefer short responses.` /
  `I prefer detailed answers.` (answers/responses are interchangeable).
- `I prefer bullet points.` / `I prefer paragraphs.`
- `I prefer metric units.` / `I prefer imperial units.`
- `Please suggest a daily planning routine.` (a descriptive suggestion only).

Case and final punctuation are flexible. Unsupported text is left unlearned.
This is deterministic extraction, not a claim of general model-based preference
inference. It reads the saved, account-owned **user** message itself; callers cannot
invent a quote or pass tool/assistant output as evidence. Sensitive traits,
credentials, quoted instructions and mixed/injected messages are not accepted by
this grammar. Routine suggestions never enter an executor or create a schedule.

`GET /api/memories` provides active records, revision, canonical value,
`allowed_values` and `sources` (message IDs, exact quotes or explicit corrections).
`PUT /api/memories/{id}` accepts `new_content` as one of that record's allowed values
and `expected_revision`. Corrections outrank later inferred messages.
`DELETE /api/memories/{id}?expected_revision=N` deletes the derived record and its
evidence/corrections, adds a non-content suppression tombstone for the topic, and
invalidates stale learning jobs. Foreign/missing records return 404; stale revisions
return 409. Historical chat remains saved. Older backups may still contain deleted
material; backup retention/recovery policy is not yet complete.

`GET /api/memory/settings` returns `learning_enabled`, `retrieval_enabled`, `epoch`,
supported values and suppressed topics. `PUT /api/memory/settings` requires both
booleans and the read `expected_epoch`; a stale tab receives 409 and cannot silently
re-enable a disabled setting. Both flags default on for this curated vocabulary.
Disabling learning stops extraction/enqueueing; disabling retrieval removes saved
preferences from future model context. Inspection/correction/deletion remain
available while disabled. Both flags off disables extraction and application.
Already-issued model requests may contain an earlier context snapshot.

Corrections, deletions and settings changes advance the account learning epoch
and invalidate queued stale jobs. Only curated preference values with owned
evidence are used in later runs; arbitrary legacy memories stay inspectable but
are not trusted for personalization. Routine suggestions are display-only.
Deleted topics stay suppressed for future messages as well as old evidence;
there is currently no UI/API to lift that suppression. Reintroducing a topic needs
a separately reviewed recovery control rather than automatic regeneration.

Memory controls and learning results are journaled in the same transaction.
Evidence/correction contents are kept in owned storage, not copied into operational
logs or journal arguments. The browser shows evidence, supported-value correction,
revision-aware deletion, suppression topics and separate learning/use switches.
Use Refresh memories after an idle learning pass. Source-message navigation and
actual browser automation remain to be verified.

## Encrypted backups and clean recovery

Use `uv run --locked python -m app.backup --help` for local keygen/create/restore.
See [the backup/restore runbook](../docs/backup-restore.md) for exact commands,
limits, separate key storage, grant renewal and recovery review. Restore refuses
existing destinations, revokes sessions, disables grants and learning, and
quarantines unfinished work. The repeatable synthetic CLI drill is
`uv run --locked --extra dev python ../scripts/verify-backup-restore.py`.

Private native release and readiness: [deployment runbook](../docs/private-deployment.md).

Reviewed MCP foundation and unverified boundaries: [MCP connectors](../docs/mcp-connectors.md).

Safe public document retrieval and its permission/verification limits are documented in [web retrieval](../docs/web-retrieval.md). Use the [fixed SQLite runtime wrapper](../docs/sqlite-runtime.md) for application and maintenance commands.
