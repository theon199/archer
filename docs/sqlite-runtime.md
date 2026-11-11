# Private fixed SQLite runtime

For the planned ARM container deployment, the host-built library cannot be loaded into the pinned Debian bookworm Python image because it requires GLIBC 2.38. `scripts/build-container-sqlite.py --destination /absolute/new-runtime` builds the same checksum-verified SQLite source inside a digest-pinned Debian GCC container with no network, then probes it in the pinned Python container. The [November 7 receipt](evidence/container-sqlite-20261107.json) records SQLite 3.54.1, the expected source ID, serialized thread safety, image digests, and library hash. This establishes library compatibility only, not an application image or Compose deployment.

Archer's API/worker/launcher now reject SQLite versions without the documented WAL-reset fix and require serialized thread safety. This DGX's unchanged host Python 3.12.3 links SQLite 3.45.1; use the private runtime wrapper below for application and maintenance commands. No host library is installed or replaced.

The pinned candidate is **SQLite 3.54.1**, released October 16, 2026. Current [official downloads](https://www.sqlite.org/download.html), [release hashes](https://www.sqlite.org/releaselog/3_54_1.html), [compilation guidance](https://www.sqlite.org/howtocompile.html), and [WAL-reset notice](https://www.sqlite.org/wal.html) were checked October 24. The builder verifies both the official archive SHA3-256 and sqlite3.c SHA3-256 before compiling, then checks version, source ID and Python thread safety. It records source URL/hashes, compiler, flags, compile options and library SHA256. No Python or JavaScript dependency changed; keep the existing uv/pnpm lockfiles.

From the repository root:

```sh
uv run --project backend --locked --extra dev python scripts/build-sqlite-runtime.py \
  --destination /absolute/private-sqlite-3.54.1
python3 scripts/with-sqlite-runtime.py --runtime /absolute/private-sqlite-3.54.1 -- \
  uv run --project backend --locked --extra dev pytest backend/tests -q
```

A local C compiler and HTTPS access to the pinned 2.85 MiB official archive are required. Source download is bounded to 4 MiB and compilation to 180 seconds. Existing destinations are rejected. Protect runtime directories/manifests from untrusted writes; hashes are integrity receipts, not signatures. The wrapper verifies architecture/library digest, replaces LD_LIBRARY_PATH for that command and its children, and executes the requested command. It does not alter the caller's environment, system loader configuration, services or host packages. It requires dynamically linked Linux CPython; static/other-platform runtimes have not been validated.

For repeat source-tree maintenance, define this function in the current shell using your actual absolute paths:

```sh
archer_uv() {
  python3 /absolute/archer/scripts/with-sqlite-runtime.py \
    --runtime /absolute/private-sqlite-3.54.1 -- uv "$@"
}
```

Use `archer_uv` wherever older runbooks say `uv`, including owner provisioning/recovery, backup/restore, legacy migration, worker, tests and verification. The helper preserves all command arguments. Run from the directory specified by each command. For a release, use its bundled wrapper/runtime instead, as in [private deployment](private-deployment.md). A Python version match alone does not establish the SQLite version.

Native releases bundle the shared library, runtime receipt and wrapper. Their manifest binds the actual mapped library's SHA256, source ID and SQLite version; the launcher checks this identity plus every payload hash before starting children. Older manifests must be rebuilt. A matching source ID alone is insufficient. Maintenance scripts do not all independently enforce this startup gate, so use the wrapper consistently.

## Verified scope

Two actual source builds on this DGX with the same compiler/flags produced identical library bytes: SHA256 `737de513824c66bb0d604475f3505506c8d8c7ba24bf126d8a454776bb840662`. This does not promise byte-identical output with different toolchains. Runtime identity/tamper/startup checks passed 9/9; note transaction SIGKILL/restart passed 25/25; real AESGCM backup/CLI restore passed 20/20; fresh locked native bundle cold start/restart passed 12/12. Receipts are `docs/evidence/checkpoint-21-{sqlite-runtime,note-crash,backup-restore,native-deployment}.json`. All use disposable synthetic user data. Native readiness checks actual local Ollama metadata/digest, not inference.

A fixed version is present and validated for this private launch path. Host power-loss/storage failures, ARM Compose, off-device recovery, full model/GPU provenance and the remaining V1 integration/remote demonstration gates remain open. No production data was upgraded or migrated and no existing service was restarted.
