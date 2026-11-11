# Private native deployment (verified subset)

A November 7 bundle including the reliability and search-client updates passed 12/12 synthetic private cold-start/restart checks; see [the receipt](evidence/v1-reliability-final-native-20261107.json). This verifies the native subset only, not Compose, remote access, search credentials, browser behavior, or model generation.

The user's V1 instance runs as the user service `archer-v1.service`, using `config/systemd/archer-v1.service`, the fresh data root and `release-v1-markdown-20261108`. It binds only `127.0.0.1:8000`; local frontend and readiness checks succeeded. The owner configured the tailnet-only route at `http://spark-9584.tailaf269b.ts.net:18080`; the host confirmed that the route reaches Archer and denies unauthenticated data access, and the owner confirmed external sign-in, chat, note creation and cited research. HTTP is carried inside Tailscale's encrypted network; browser HTTPS is not configured. The current bundle's synthetic cold-start/restart checks are recorded in [this receipt](evidence/v1-markdown-native-20261108.json).

Archer is a general personal assistant. This procedure builds its existing web/Core application for this ARM64 DGX, with a separate durable worker and the existing loopback Ollama service. It installs no host service, scheduler, proxy or model. Keep data, credentials and model weights outside the release. Compose, pinned container images, browser acceptance and full GPU/model provenance are still open V1 requirements; this native bundle is not evidence for those requirements.

Prerequisites: the tested Python **3.12.3**, Node **24.22.1**, pnpm **12.14.0**, and uv (exact build version recorded in the generated manifest), and existing Ollama **0.34.2** on `127.0.0.1:11434`. Do not upgrade/download models just to follow this guide. Existing `gemma4:26b` digest observed here is `08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68`; its complete template/GPU provenance remains unverified. Use the reviewed digest of your own approved artifact; a mismatch must fail readiness. No cloud fallback is enabled.

First build the [pinned private SQLite runtime](sqlite-runtime.md). The unchanged host SQLite 3.45.1 is rejected at startup. From repository root, build into a new directory whose parent already exists:

```bash
python3 scripts/with-sqlite-runtime.py --runtime /absolute/private-sqlite-3.54.1 -- \
  uv run --project backend --locked --extra dev python scripts/build-native-release.py \
  --destination /absolute/new-release --sqlite-runtime /absolute/private-sqlite-3.54.1
cd /absolute/new-release/backend
uv sync --locked
```

The builder uses `pnpm install --frozen-lockfile` and the existing `uv.lock`. It copies application Python, backend package/lock/README, built static files, the private SQLite shared library/receipt and its command wrapper. `release-manifest.json` records architecture, tool versions and SHA256 of each payload file; the launcher checks every listed hash, Python version and the actual loaded SQLite version/source ID/library digest before starting. The manifest is an integrity receipt, not a signature or a complete host/model image. Protect the release from writes by untrusted accounts. Dependencies use the frozen lock; OS libraries and inference engine are external prerequisites. Destination overwrite is rejected.

Set these in the shell used both for provisioning and running (example paths must be changed). Use an existing owned private directory; apply `umask 077` before creating data/configuration. Do not place credentials in shell command arguments or Git.

```bash
umask 077
export ARCHER_ENV=production
export ARCHER_HOST=127.0.0.1
export ARCHER_PORT=8000
export ARCHER_DATA_DIR=/absolute/private-runtime/data
export ARCHER_APPROVED_FILES_ROOT=/absolute/private-runtime/files
export ARCHER_STATIC_DIR=/absolute/new-release/frontend/dist
export ARCHER_MODEL_PROVIDER=local-ollama
export ARCHER_OLLAMA_BASE_URL=http://127.0.0.1:11434
export ARCHER_OLLAMA_MODEL=gemma4:26b
export ARCHER_OLLAMA_NUM_CTX=4096
export ARCHER_EXPECTED_MODEL_DIGEST=08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68
archer_uv() {
  python3 /absolute/new-release/bin/with-sqlite-runtime.py \
    --runtime /absolute/new-release/runtime/sqlite -- uv "$@"
}
archer_uv run --locked python -m app.auth
archer_uv run --locked python -m app.serve
```

Provisioning prompts for a new owner and password and refuses a second owner. Skip provisioning when restarting existing data. The launcher runs API and worker in the foreground and terminates only its children on SIGTERM/Ctrl-C or either child's exit; it does not automatically restart. A fresh launch performs durable-worker recovery, including quarantine of uncertain actions. It binds loopback and rejects cloud providers and non-loopback model URLs. No host networking/firewall changes are needed. Access locally at `http://127.0.0.1:8000`; no public endpoint is configured.

`GET /api/ready` returns 200 only with a working SQLite query, a worker heartbeat younger than five seconds, and the configured model present at the local metadata endpoint (matching the digest when pinned). Otherwise it returns 503 with booleans. A stopped worker can remain reported fresh for at most five seconds. Readiness does not run generation or measure GPU residency. `/api/health` is liveness metadata only. API responses are `no-store`; frontend files and API share an origin. Sign in before using data/tools; explicitly grant required actions. Missing search credentials leave search unavailable.

Stop with Ctrl-C before replacing release files. Preserve runtime paths and use a new bundle path for upgrades. Check the new manifest and `/api/ready`; validate an owner login and saved conversation before declaring recovery. Keep rollback snapshots with their original release manifests; schema downgrades are not supported. Follow [backup/restore](backup-restore.md) before upgrading or recovery; restoring revokes sessions/grants and quarantines unfinished work. Do not run both old and new workers on the same data.

Repeat the bounded synthetic real-process drill from source after building/installing a bundle:

```bash
python3 /absolute/new-release/bin/with-sqlite-runtime.py \
  --runtime /absolute/new-release/runtime/sqlite -- \
  uv run --project backend --locked --extra dev python scripts/verify-native-deployment.py \
  --release /absolute/new-release --evidence /absolute/new-evidence.json
```

It uses temporary owner/database files, the real local model **metadata**, real HTTP, clean stop and restart. It does not generate text, execute a browser, touch production data or prove Compose deployment. Current fixed-SQLite receipt: `docs/evidence/checkpoint-21-native-deployment.json` (12 checks passed, including owned-worker failure stopping both launcher children). Prior checkpoint receipts separately cover real generation/tools/preferences. Current FastAPI file response behavior was checked against [official documentation](https://fastapi.tiangolo.com/advanced/custom-response/#fileresponse). Python/JavaScript package versions are unchanged; the private SQLite library is pinned to 3.54.1 and bundled separately. Older release receipts/manifests remain historical and must be rebuilt for current code.

The source/example context default is now4096, matching the bounded native recipe above; higher explicit overrides remain unvalidated. See [local model provenance and observed quality/resource limits](local-model-provenance.md) before treating a model maximum context or API residency figure as a deployment guarantee.
