# Reviewed MCP connectors (partial V1 implementation)

A real official read-only time connector now has sandbox review and local-model invocation evidence; see [time MCP runbook](time-mcp.md). Earlier fixture receipts below retain their explicit development-only scope. Use the [fixed SQLite wrapper](sqlite-runtime.md) for the Python/uv commands below.

Archer uses the official **MCP Python SDK 2.3.0**, pinned in `backend/uv.lock`, and jsonschema 4.26.0. Current official [SDK documentation](https://py.sdk.modelcontextprotocol.io/), [v2.3.0 source](https://github.com/modelcontextprotocol/python-sdk/tree/v2.3.0) and [published package metadata](https://pypi.org/project/mcp/2.3.0/) were checked on 2026-10-24. The [jsonschema validator documentation](https://python-jsonschema.readthedocs.io/en/stable/validate/) and [4.26.0 release](https://pypi.org/project/jsonschema/4.26.0/) were also checked. No local model/version was changed. Use `uv sync --locked`; no runtime `npx`/`uvx` downloads occur.

No connector is enabled by default. The old example with direct access to Archer's database, arbitrary downloads and broad third-party commands has been replaced with an empty list. Do not give a connector Archer's SQLite database, Docker socket, host root, shared credentials, or model endpoint.

Set `ARCHER_MCP_CONFIG_PATH` to an operator-owned regular JSON file with mode 0600. It contains a list of at most 16 fixed mappings (128 KiB maximum); it is not an API or model-controlled file. Keep it outside the release, source control and frontend. Restart both API and worker with the same reviewed file after changes. Invalid config fails startup; no silent fallback. Symlink config files are rejected. Parent-directory ownership is an operator responsibility.

Each mapping has these fields:

- `name`: fixed public name `mcp_` plus lowercase letters, digits and underscores (up to 48 suffix characters).
- `account_id`: exact existing owner account ID, assigned by the operator, never supplied by model arguments.
- `description`: operator-reviewed text shown to the model; live server descriptions never replace it.
- `remote_tool`: exactly one reviewed advertised name; arbitrary server/tool dispatch is unavailable.
- `input_schema`, `output_schema`: reviewed JSON Schema closed objects with `additionalProperties: false`; references are rejected. Input is limited to 16 KiB serialized JSON, structured output to 64 KiB after decoding. Text/image/resource result blocks are not forwarded. All returned data remains untrusted.
- `advertised_sha256`: SHA256 of the entire selected advertised Tool object after `model_dump(mode="json", by_alias=True, exclude_none=True)`, serialized with `json.dumps(value, sort_keys=True, separators=(",", ":"))`. Review descriptions, schemas and annotations out of band; annotations do not establish safety. Any change rejects invocation before calling the tool. Local input schema may narrow the advertised input contract.
- `read_only: true`: an operator assertion requiring actual code/behavior review. Writes and unknown effects are unsupported; a server annotation alone is insufficient.
- `image`: locally present immutable image reference `repository@sha256:<64 lowercase hex>` or full local Docker image ID `sha256:<64 lowercase hex>`. A local image ID identifies image configuration/content in this daemon and is distinct from a registry manifest digest; record both base manifest and built image ID for locally built fixtures. Mutable tags and truncated IDs are rejected. No pull occurs. No arbitrary arguments, mounts, environment, network or shell strings are accepted. The image entrypoint must implement stdio directly under uid/gid 65534. Production native commands and remote transports are rejected.

The production launch profile uses `/usr/bin/docker run --pull=never --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges --user=65534:65534 --pids-limit=64 --memory=256m --cpus=1`, a 16 MiB noexec/nosuid temporary filesystem, no Docker logs and no host mounts. Each invocation gets its own random container name; teardown only removes that exact owned name. Images requiring credentials, files, remote service access or writable state are currently unsupported. Docker access is an operator privilege; model arguments never enter Docker options.

**The fixed Docker profile has development-fixture coverage below and separate actual official time-server evidence in the linked runbook. Neither approves arbitrary third-party connectors or a general release.** Archer now supplies bounded stdio streams to the SDK: 128 KiB per newline-inclusive frame **before** UTF-8/JSON decoding, at most 128 messages and 2 MiB per direction per invocation. Reads use 4096-byte chunks and one-message queues; oversized/unterminated/invalid/partial frames and notification floods close the connection with payload-free errors. Input and output also retain the narrower application schema/size checks. The SDK's own default stdio transport is not used for production invocation. A five-second SDK request deadline and 15-second registry deadline remain in force.

Transport teardown cancels I/O tasks, signals the owned process group, escalates to SIGKILL after one second when needed, reaps the process and closes the complete AnyIO process/pipe resources. Repeated cancellation cannot detach the cleanup task. Tests cover a SIGTERM-resistant stdout flood, exact frame/message/byte boundaries, fragmented UTF-8, EOF, timeout and repeated cancellation. The checkpoint 8 receipt establishes direct-child transport behavior. The separate checkpoint 9 receipt exercises actual Docker/SDK behavior, observed cgroup limits, forged other-account grant denial, timeout, repeated cancellation, persistent SIGTERM-resistant roots and detached descendants. Cleanup now awaits one owned task across repeated cancellation, removes only its random exact name, and requires a successful daemon listing confirming absence. Unverified removal is recorded as `needs_reconciliation` in the tool operation and sanitized journal, and replay does not re-execute it. Both launch and cleanup use sanitized local Docker environments. The live-container presence query is checked before the absence query to guard against a false filter-based pass. No third-party account connector is configured or claimed successful.

The full mapping digest is its tool version. Changes to account, schemas, image, advertised definition or description invalidate old grants. The authenticated owner may inspect `/api/plugins` and `/api/permissions`, then explicitly `PUT /api/permissions/<fixed-name>/invoke` with `{"enabled":true}`. Other accounts cannot list, grant or execute that mapping, even with a forged database grant. Revocation is checked at admission, immediately before the remote call and again before forwarding its result. An already issued read cannot be retroactively erased. Config changes require process restart and a fresh grant; no background schema auto-approval occurs.

Each call uses the existing durable operation ID and sanitized journal. Reusing an operation ID returns its persisted result; changed arguments conflict. Interrupted operations are not automatically retried. Journal outcomes record actual success/failure/denial/cancellation with configuration/grant versions; every MCP argument value is redacted, including keys such as `id` and `action`. Server stderr is discarded and SDK transport/session diagnostics redact server payloads. Sampling, roots and elicitation callbacks are not enabled; SDK defaults reject unsolicited requests. No cloud provider is reachable through these callbacks. Read/handshake timeout is five seconds; the registry also has a 15-second overall deadline. There is no automatic retry.

For development/tests only, a mapping may replace `image` with `development_command`, an explicit fixed argument list beginning with an absolute executable. It is rejected in production. This path runs trusted fixture code as the current user, **without an OS filesystem sandbox**; it is unsuitable for untrusted servers. Ambient application secrets and inherited HOME/PATH values are not forwarded. Fixture source lives only under `backend/tests/fixtures/`; the release builder excludes it.

Reproduce the synthetic protocol demonstration from source:

```bash
uv run --project backend --locked --extra dev python scripts/verify-mcp-development.py \
  --evidence /absolute/mcp-development-evidence.json
uv run --project backend --locked --extra dev pytest -q backend/tests/test_mcp.py
```

The receipt at `docs/evidence/checkpoint-7-mcp-development.json` covers real SDK stdio/process/SQLite behavior against explicitly synthetic data. It does **not** prove a third-party integration, production Docker profile, real model tool selection, browser approval flow or network/credential isolation. The new dependencies invalidate earlier native bundle payloads: rebuild from current source before deploying; the checkpoint 6 receipt remains evidence for that earlier bundle only. Backups exclude this operator config and images; recover the separately protected reviewed configuration, then explicitly renew restored disabled grants.

Bounded transport verification (explicit synthetic adversaries; no production services):

```bash
cd backend
uv run --locked --extra dev pytest -q tests/test_mcp_transport.py tests/test_mcp.py \
  -W error::pytest.PytestUnraisableExceptionWarning \
  --junitxml=../docs/evidence/checkpoint-8-mcp-transport-tests.xml
uv run --locked --extra dev python ../scripts/verify-mcp-development.py \
  --evidence ../docs/evidence/checkpoint-8-mcp-development.json
```

The JUnit receipt records 23 focused checks, including retained file descriptor/task checks across six launches; the separate application receipt records 13 real SDK/SQLite development checks through the bounded transport. AnyIO **4.15.1**, already present in the existing lock, is now a direct exact dependency. Its [official subprocess lifecycle docs](https://anyio.readthedocs.io/en/stable/subprocesses.html) and [release metadata](https://pypi.org/project/anyio/4.15.1/) were checked. SDK 2.3.0 custom stream/cancellation documentation was rechecked; no model or SDK upgrade occurred. These receipts close the tested framing/child cleanup gap only. These earlier receipts did not cover Docker; the development container drill below adds bounded evidence for that fixed launch profile. Those earlier transport receipts do not establish credentialed connectors, real-model selection or browser behavior. Later owner-control and official-time receipts provide their separately scoped evidence.

Actual Docker boundary drill (explicit development image only):

```bash
# From repository root, with the documented base already present locally:
docker build --pull=false --network=none \
  --iidfile /tmp/archer-mcp-development-image-id \
  --tag archer-mcp-development-fixture:local \
  backend/tests/fixtures/mcp_container
# Read the image ID file, then pass its exact sha256:... value (never the tag):
uv run --project backend --locked --extra dev python scripts/verify-mcp-container.py \
  --image sha256:<exact-image-id> --evidence /absolute/container-drill.json
```

This build uses only a cached base and COPY, and does not publish an image. `COPY --chmod=0555` makes both the source and Docker-created parent directory usable by uid 65534. Source is intentionally under `backend/tests/fixtures/mcp_container`, labeled development-only and excluded from application release bundles. The verifier refuses an image lacking that fixture label and uses temporary synthetic accounts/databases. It tests the real image launch path with `settings.env=production`; it does not install a synthetic connector into production configuration.

Recorded fixture image ID: `sha256:5f42afa230bebe9b9556991ae8f69783bdf53f0e00d4dce13c59ff96e5746916`. Base manifest: `node@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205` (existing ARM64 Node 24.22.1 image; no upgrade). Receipt: `docs/evidence/checkpoint-9-mcp-container.json` (**35 checks passed**), including source hashes and actual in-container probe values. `checkpoint-9-container-summary.json` records tested source/lock hashes and Docker Engine **29.2.1**, API 1.53, linux/arm64. Full image IDs are local-daemon identities; save/recover an approved image separately if needed. A fresh rebuild may have a different image ID and must be reviewed/regranted accordingly.

The drill inspects actual daemon policy plus uid 65534, EROFS on a normally writable root path, writable temporary storage, blocked host loopback, absent host source/Docker socket, no capabilities, no-new-privileges and cgroup memory/pids/CPU values. It does not stress memory exhaustion, process exhaustion or CPU throttling. No existing unrelated container is stopped, and no host driver/network configuration is changed. The local fixture image is retained for inspection; verifier containers are removed by exact name.

Durable crash recovery now records each image invocation in `mcp_resources` **before** launching Docker: account, durable tool operation, pinned image reference, worker owner, random name and per-invocation identity token. Image execution requires the active worker lock for the same database, and stale inherited contexts are rejected after that lock closes. At most 32 pending resources may exist; reaching that bound fails closed. No extra scheduler or automatic tool retry is installed.

Each image carries `archer.resource-token` and `archer.worker-owner` labels. Normal cleanup and startup recovery inspect the exact recorded name, require matching token/owner/image, then remove the inspected immutable container ID. They confirm absence before deleting the pending record. A daemon failure or identity mismatch keeps the record and prevents worker startup from executing more work; inspect that exact record/container manually. No prefix scan/removal or daemon pruning is used. Successful resource cleanup is a separate journal action: it does not establish that the interrupted original read completed.

The replacement worker acquires the existing exclusive database worker lock, recovers interrupted runs, marks the original operation/journal `needs_reconciliation`, and reconciles pending resources before claiming work or reporting a heartbeat. The original result remains unknown and is never replayed. With no pending resources this path needs no Docker access, so optional connector availability cannot block an otherwise clean deployment.

Backup restore marks saved resource rows `restored_unverified`. They are retained for review and **excluded** from automatic cleanup, because the source deployment may still own those containers. Restoring a snapshot must not delete source resources even when their old labels match. Restored grants remain disabled as before. Do not simply turn restored rows back into pending; reconcile the source/target identities explicitly. Old snapshots lacking this table still restore.

Actual crash evidence is limited to isolated process SIGKILL/restart on this DGX. Host power loss, daemon restart and hardware/storage failures have not been induced; identity mismatch fails closed rather than proving recovery from every infrastructure failure. Full untrusted-connector and third-party release approval remains open.

Current Docker [run/resource documentation](https://docs.docker.com/engine/containers/run/) and [network-none documentation](https://docs.docker.com/engine/network/drivers/none/) were rechecked using find-docs and official pages for this drill. The application/worker Compose release, full model/runtime provenance, broader account integrations and the remaining V1 demonstrations are still separate acceptance gates. The specific real official time connector is documented separately.

Crash/restart verification (explicit development fixture and synthetic tool proposal):

```bash
uv run --project backend --locked --extra dev python scripts/verify-mcp-crash.py \
  --image sha256:5f42afa230bebe9b9556991ae8f69783bdf53f0e00d4dce13c59ff96e5746916 \
  --evidence /absolute/crash-evidence.json
cd backend
uv run --locked --extra dev pytest -q tests/test_mcp_ownership.py tests/test_backup.py
```

The verifier first repeats the real fixed-profile sandbox checks. It then SIGKILLs an owned process holding the real Worker lock and executing a real SDK/Docker read, confirms that its stubborn container and detached descendants survive, and starts **the real `app.runs.worker`** against the same synthetic SQLite data. It checks exact orphan removal, durable uncertainty/recovery journal and no read replay. A separately created negative-control container with a mismatched label is left running and its record retained by recovery; only the verifier's known actual identity is then used to remove its own control. No unrelated user container is touched. `tests/fixtures/mcp_crash_driver.py` deliberately supplies a synthetic tool proposal, so this is **not** model-selection/inference evidence. Receipt `docs/evidence/checkpoint-10-mcp-crash.json` records 16 successful checks; the summary records exact tested source hashes. No production data/configuration is used.

Ownership labels and inspect behavior were rechecked against current official [Docker object-label documentation](https://docs.docker.com/engine/manage-resources/labels/) and [container inspect reference](https://docs.docker.com/reference/cli/docker/container/inspect/). Labels are static metadata, not sufficient authority by themselves: cleanup compares them to the protected pre-launch database record and pinned image reference. No dependency/model version changed for this increment.
