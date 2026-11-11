# ARM Compose verification (partial)

The running V1 service remains the native user service. The Compose stack in `config/compose/compose.yaml` is an isolated candidate, not a production replacement: it does not yet support the reviewed Docker-backed MCP connector, has not been exercised through Tailscale or a browser, and does not have production supervision or a complete release manifest.

The app and worker share a private network namespace with a model bridge listening only on its loopback interface. A host-owned Unix socket connects that bridge to the existing loopback Ollama endpoint. The app serves a second Unix socket in the same private directory. No container port is published. All containers use the operator's non-root UID, read-only root filesystems, dropped capabilities, and no-new-privileges. The approved-files mount is read-only; the bridge, data, and approved directories must be owned by the operator and mode 0700. The model socket must be owned and mode 0600. No Docker socket is mounted into any container. A host-side relay has the authority to access the model and must be managed and protected by the operator.

Build the container-compatible SQLite library and app image from the repository root:

```sh
python3 scripts/with-sqlite-runtime.py --runtime /path/to/host-fixed-sqlite -- \
  uv run --project backend --locked --extra dev python scripts/build-container-sqlite.py \
  --destination /path/to/new-container-sqlite
python3 scripts/build-compose-image.py --sqlite-runtime /path/to/new-container-sqlite --tag archer-v1:local-test
```

Both pinned base images must already be present for the isolated SQLite build. It verifies the upstream source checksums, compiles with the pinned Debian GCC image, and probes the result inside the pinned Python image. The app builder exports hash-locked dependencies into a temporary build context containing only app code, built frontend and the verified SQLite runtime. Its image ID, not its mutable tag, is used for the verification below. Package installation requires network access during image build.

```sh
python3 scripts/with-sqlite-runtime.py --runtime /path/to/host-fixed-sqlite -- \
  uv run --project backend --locked --extra dev python scripts/verify-compose.py \
  --image sha256:REVIEWED_LOCAL_IMAGE_ID --evidence /path/to/new-receipt.json
```

The verifier starts its own short-lived host relay and Compose project, creates synthetic data and owner credentials, and makes a real local model request. It checks a refused insecure socket directory, readiness, authentication, saved run, idempotence, no published ports, and survival of an app/worker restart; it removes only its own project and temporary data. It does not change the native service. The [guarded run receipt](evidence/compose-guarded-20261107.json) records those checks. It does not establish GPU termination, real user data recovery, browser access, or reviewed MCP connectivity. The [first image attempt](evidence/compose-attempt-20261107.json) failed because copied source files were unreadable by the non-root container; the build now normalizes modes before image creation.
