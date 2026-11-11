# Owner tool permissions

The owner selected the current notes actions (including delete), approved-file list/read, Tavily search, public-document retrieval, and the configured read-only time connector to be enabled by default. Initial setup applies this choice once to the owner account and records it in the journal. Actions the owner previously disabled stay disabled; later revocations stay disabled. Other connectors are not automatically included. A changed tool version is not automatically granted after the initial application. Model output cannot alter these settings.

Sign in and open **Tool permissions** in the sidebar. The panel lists your registered notes/files/search/public-document capabilities and reviewed MCP mappings for your account. Nothing in this panel installs a connector, changes server configuration, creates an API key, or invokes a tool. Grants apply only to the selected action and current reviewed version. MCP writes remain unsupported.

Each action shows **Not allowed**, **Allowed**, or **Review required: tool version changed**. Inspect the reviewed version disclosure and description before allowing. A saved grant for a different version is not current authority. Revoking a stale grant is permitted after refreshing to the current catalog. Configuration checks remain separate: missing Tavily credentials are labeled unavailable even if you choose to grant search. Public retrieval sends the requested URL to that website; search sends the query to Tavily.

New controls send the exact catalog version with every change. The server rejects stale versions with HTTP 409 before changing a row or recording a successful permission change. Older note/API clients may omit expected_tool_version for compatibility; they still require owner authentication and record authority for the current tool version. This compatibility path is not a guarantee that every external owner client reviewed a fingerprint. Models cannot grant permissions.

After each change, the panel reloads from the server before showing the authoritative state. A lost acknowledgement triggers a read instead of an automatic write retry. If that read fails or owner access is denied, cached controls disappear and Refresh permissions remains available. Active panels refresh on focus, visibility changes, every five seconds while visible, and same-origin cross-tab invalidation. The cross-tab notification contains only a refresh signal, never a token or grant snapshot. Requests use cancellation plus a generation check so older responses cannot replace newer state. Inactive/closed panels do not poll. Cached note cards/edit/history are cleared when navigating to notes and then fetched under current grants.

## API

Owner-only `GET /api/permissions` retains available/tool_version/grants for compatibility and adds capabilities with name, actual per-tool version, description, actions and optional configuration_notice. Versions of reviewed MCP mappings are their full mapping fingerprints; built-in version1 is not substituted for them. Other accounts' mappings are excluded.

Owner-only `PUT /api/permissions/{tool}/{action}` accepts enabled (boolean) and optional expected_tool_version (bounded string). Version mismatch returns 409; unknown/foreign mappings return 404; nonowners return 403. Successful writes and grant revisions are atomic with the existing permission_change journal. Failed version comparisons leave both row and journal unchanged. See the existing registry for grant/version enforcement during tool dispatch.

## Actual verification

Use the [fixed SQLite wrapper](sqlite-runtime.md), existing lockfiles and installed Chrome. From repository root:

```sh
pnpm --dir frontend build
archer_uv run --project backend --locked --extra dev python scripts/verify-browser-history.py --scenario permissions
```

The actual Chrome/private API/worker/SQLite drill passed **25/25 checks** at **2026-10-24 16:58:59 UTC**. Evidence: `docs/evidence/checkpoint-23-browser-permissions.json` and `.png`. It verifies owner-only catalog filtering, stale built-in and MCP versions, exact version submission/current persisted grant, 409 refusal, a delayed real GET snapshot failing to restore a revoked grant, file grant controls, a mapping fingerprint grant, a real committed search grant with its acknowledgement dropped and no duplicate write, two owner tabs with revoke refresh, failed-read control clearing/recovery, real note API allow/deny, nonowner control refusal and no foreign mapping disclosure. No application errors or external browser requests occurred.

The MCP mappings in this browser drill are explicitly synthetic metadata with nonexecuted image identities. No container/MCP server was launched and no useful third-party MCP integration is claimed. File grants are verified, not a new file-read integration; search grants are verified, not credentialed Brave search. No model inference occurs in this drill. Prior real inference/tool receipts retain their separate scope. All credentials/data are temporary synthetic fixtures; only owned app/worker/browser children are stopped.

Backend regression **181 passed in 28.20s**, including SQL/auth catalog tests for current MCP versions, foreign mapping filtering, version refusal without row/journal changes and nonowner rejection. Frontend build and three protocol tests passed. Lint retains two pre-existing nonfatal set-state-in-effect warnings in App/ChatPanel; the new panel has no remaining warning. Current React cleanup/race guidance was checked against [official React documentation](https://react.dev/learn/synchronizing-with-effects), with installed React19.2.8/Vite8.3.4/Playwright-core1.64.1 unchanged. The React review and full-story verification skills were applied locally; no Vercel service was accessed.

The five-second refresh is not a real-time revocation notification guarantee; backend checks remain authoritative. Lost responses can leave a changed permission that must be inspected, even if the panel was closed. This feature does not establish third-party connector safety, credentialed search, Compose, off-device restore, model/GPU provenance or private remote access.

Fresh native bundle `build/release-checkpoint-23` passed 12/12 private cold-start/restart checks at 17:01:31 UTC; receipt `docs/evidence/checkpoint-23-native-deployment.json`. It verifies current payload/startup with fixed SQLite and actual local model metadata, not generation.
