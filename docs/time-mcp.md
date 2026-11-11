# Reviewed official time MCP connector

On November 7 the reviewed mapping was configured for the fresh V1 owner at `/home/sri/.config/archer/mcp.json`, and the native service was restarted with that path. The owner later selected this connector's invoke action for default enablement; it can still be revoked in Tools. Compose does not yet support this Docker-backed connector.

Archer now has a tested read-only third-party MCP path for checking the date/time/weekday in America/Los_Angeles or UTC before everyday planning. It uses the official reference **mcp-server-time 2026.10.19**, not the synthetic fixture used by earlier sandbox tests. It needs no account key, host mount, network, message sending or scheduler. The returned time comes from the host/container clock and pinned timezone data, not an independent trusted time service.

Current official [server README/source](https://github.com/modelcontextprotocol/servers/tree/main/src/time), [package metadata](https://pypi.org/project/mcp-server-time/2026.10.19/) and [official Python image](https://hub.docker.com/_/python) were checked October 24, 2026. The exact published wheel SHA256 is `5c099a001c6ca78260bda419f2e9b2aabee1adc4fe6ca56d8959871ad8da58a1`; its Python source was read and hashed before review. The selected function calls datetime/zoneinfo, returning timezone, ISO datetime, weekday and DST flag. Server annotations alone are not authority. `convert_time` is advertised by the server but is not mapped or granted in this initial profile.

## Build and review

Use the existing uv lockfile and [fixed SQLite wrapper](sqlite-runtime.md). From repository root:

```sh
archer_uv run --project backend --locked --extra dev python scripts/build-time-mcp.py \
  --evidence /absolute/time-build.json
archer_uv run --project backend --locked --extra dev python scripts/review-time-mcp.py \
  --image sha256:69495d869de338fe9a1aead2c488a9b80cf95815c07c221cd3495ef26ed543a4 \
  --evidence /absolute/time-review.json
archer_uv run --project backend --locked --extra dev python scripts/verify-time-mcp.py
```

The tracked Dockerfile uses official ARM64 Python3.12.15 bookworm-slim manifest `sha256:c3729439afc4ec3f3e159e0c86aeabfadac8030ef46838a0256383d0511c5f5e`. The builder exports hash-pinned binary requirements from the existing uv.lock with optional time-mcp extra, then pip verifies every hash during image construction. No independent resolver/lockfile or runtime download is used. Optional packages added are mcp-server-time2026.10.19, [tzdata2026.5](https://pypi.org/project/tzdata/2026.5/) and [tzlocal5.4.5](https://pypi.org/project/tzlocal/5.4.5/), with current official package metadata rechecked; existing locked versions remain. The image contains the locked backend dependency closure for lock reuse, but no Archer application/database/credential tree. It is a time-connector image, not the application/worker Compose image. PYTHONTZPATH is empty so zoneinfo uses pinned tzdata instead of mutable system zoneinfo; the server's explicit fallback is UTC.

Build network access downloads only packages/base layers during the bounded build; runtime network is none. The current image is **226,539,299 bytes**, local image ID `sha256:69495d869de338fe9a1aead2c488a9b80cf95815c07c221cd3495ef26ed543a4`. A repeated tracked build reused that exact image through Docker cache. This is not a clean-room byte-reproducibility or signature claim. New output IDs require new review/configuration; never use the mutable tag in a mapping or overwrite the pinned digest after an upgrade. Neither image nor package is published by these scripts.

The complete selected advertised Tool object fingerprint is `5b98860a84ca3152d9b0881b98c7e7b13e0bd7ac92bded6bc614e35f06455948`. The live schema has no references and supplies structuredContent. Local schemas in `config/mcp-time/get-current-time.json.example` are deliberately narrower, closed/bounded and permit only the two reviewed timezones. A timezone/config/image/schema change requires re-review, a new mapping version and owner grant. Output is untrusted data, not instructions or an automatic reminder.

## Operator configuration

The example is one mapping object. Replace its account placeholder with the exact existing owner ID and put it inside an array in your separately owned mode0600 ARCHER_MCP_CONFIG_PATH file, following [MCP configuration](mcp-connectors.md). Do not overwrite other reviewed mappings or store credentials/config in the frontend. Keep the exact reviewed local image ID and advertised fingerprint. Both API and worker must load the same file on their next authorized private restart. No production configuration or owner data was changed by this checkpoint.

In the browser, open Tool permissions to inspect or revoke the connector. The selected current mapping is enabled by the owner's initial default choice; changed mapping versions require review. A mapping's version includes its owner account ID, so the disposable verifier's mapping version is not a production version to copy. Ask for `mcp_time_current` with an explicit allowed timezone. Missing grants, foreign mapping access, unreviewed timezone arguments, advertised fingerprint mismatch and revoked cached reads fail before an authorized result is returned. No arbitrary tool/command/container options enter model arguments.

Runtime uses the existing production sandbox unchanged: no network/host mounts/ports, read-only root, uid/gid65534, all caps dropped, no-new-privileges, 256MiB memory/one CPU/64 PIDs and 16MiB noexec temporary filesystem. Full image ID is checked locally with pull=never. Runtime invocations require the real worker lock and a pre-launch durable resource record. Exact labels/image identity are verified before deletion, absence is checked, then the pending resource row is removed. Only that invocation's exact container is removed; no prefix bulk removal or unrelated service stop. Review discovery is a separate bounded maintenance command with exact-container cleanup, not a worker crash-recovery proof.

## Evidence and limitations

- Official server sandbox review **14/14 passed**, 2026-10-24 17:08:09 UTC: live SDK/version/structured time, host-clock agreement, reviewed no-reference advertisement, actual daemon isolation/resource settings, invalid timezone isError, exact-container removal. Receipt checkpoint-24-time-review.json.
- Actual registry/auth/worker/SQLite/official Docker server/local Ollama integration **21/21 passed**, 17:10:54 UTC: default deny/current owner version grant, real LA clock/weekday, durable replay without a new launch, foreign/path-like timezone rejection, changed expected advertisement rejection, exact local model digest, real model selection and successful grounded date/weekday answer, accurate/redacted journal, no pending resources and all exact containers absent, revoke-before-cached-result denial. Receipt checkpoint-24-time-integration.json. It made exactly three runtime launches: direct reviewed read, intentional expected-advertisement mismatch and actual model read. Returned clocks, server bytes and model results were never mocked.
- The model was the existing local gemma4:26b, digest `08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68`. Its final answer was “Today is Saturday, 2026-10-24 in the America/Los_Angeles timezone.” The synthetic owner/database were removed; no personal content was read. Worker ran in-process under its actual lock; no separate-process restart or browser grant claim from this drill.
- Initial review flagged public Python base-image GPG_KEY metadata with an overbroad key-name check; corrected to the explicit known public image environment-name set. Initial integration verifier queried nonexistent resource state/removed rows; corrected to actual pending status/deletion and exact daemon absence checks. Both failed receipts remain labeled `*-initial-failed.json`. No failure was relabeled as success.

The selected expected-advertisement mismatch is explicit verifier fault injection; it does not mean the official server changed its schema live. Earlier synthetic transport/cancellation/orphan-restart receipts retain their separate scopes. This real connector proves the selected clock capability; it does not validate arbitrary MCP servers, account/credential integrations, writes, mutable host data, time synchronization, host power loss, external search, Compose, off-device recovery or private remote use. Full V1 remains unfinished.

Fresh native main-app bundle build/release-checkpoint-24 passed 12/12 private cold-start/restart checks at 17:13:54 UTC (`checkpoint-24-native-deployment.json`) with the new optional lock entries and prompt guidance. It does not automatically configure/grant this connector.
