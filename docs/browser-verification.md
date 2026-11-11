# Saved chat and browser verification

The chat view loads owned conversations from the durable API. Selecting a conversation puts only its opaque ID in `?conversation=...`. Reload requires signing in again because the opaque session token stays in React memory; it is not stored in localStorage, sessionStorage, cookies or the URL. After sign-in the selected conversation loads automatically. Sign-out removes the authenticated view.

`GET /api/v1/conversations/{id}/history` returns the latest 200 persisted messages plus canonical assistant output/status for associated runs, including canceled and failed partial output. `older_before` pages earlier messages. Output and event sequence are read in one SQLite SELECT snapshot, so resuming after that watermark neither repeats the saved prefix nor skips later committed events. The frontend merges message IDs across pages. The conversation picker currently lists the latest 100 conversations; older-conversation discovery beyond that bound remains unfinished.

Leaving a conversation, changing tabs, signing out or reloading aborts the view's requests and SSE reader. The durable run continues. The chat view retries interrupted streams four times with the last processed event sequence, then offers **Reconnect saved run**. This action reloads saved state, never submits another user message. **Cancel run** is a separate explicit server action. A submission with uncertain acknowledgment can be retried with the same client operation ID. Navigating away clears the pending view; its conversation can be recovered from saved chats.

## Reproduce the local browser drill

Prerequisites: existing local Ollama serving the configured `gemma4:26b` digest, installed `/usr/bin/google-chrome`, locked frontend/backend dependencies, and no public deployment. The verifier uses the existing model without downloads or upgrades. Its pinned expected model digest is in `scripts/verify-browser-history.py`; revalidate any change against official model documentation and local evidence.

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
cd backend
uv run --locked --extra dev python ../scripts/verify-browser-history.py
```

`playwright-core` is an exact dev dependency (1.64.1) in the existing pnpm lockfile. No browser is downloaded. Playwright cautions that arbitrary custom browser executables are not guaranteed compatible; the receipt records the installed Chrome version actually tested. See [Playwright browser launch](https://playwright.dev/docs/api/class-browsertype#browser-type-launch) and [React effect cleanup](https://react.dev/reference/react/useEffect#connecting-to-an-external-system).

The harness creates a private temporary database, random owner credentials, approved-files root and empty MCP configuration; starts its own API/worker launcher on a free loopback port; drives installed Chrome; and terminates only its own launcher/processes. Browser application requests outside that origin are aborted. This does not prove that every browser or host background process lacks network access. Temporary credentials never enter receipts/screenshots and the temporary directory is removed afterward. All seeded history and the second account are explicitly synthetic. Inference uses the actual production local provider, not a fake adapter.

Evidence: `docs/evidence/checkpoint-11-browser-history.json`, screenshot `checkpoint-11-browser-history.png`, and source/version hashes in `checkpoint-11-history-summary.json`. Checks cover sign-in, saved history, reload during an active queued/generating run, fresh sign-in and selected-conversation restoration, real local-model output, no duplicate submission/rendered assistant, terminal status rendering, no browser-stored credentials, navigation, durable cancellation, other-account denial, logout, application errors and origin restrictions.

Limits: cancellation here proves persisted app cancellation, not termination of upstream GPU generation. Reload is a real connection interruption; deliberate packet loss, upstream timeout/provider failure and restart during a write still need separate real-model drills. Browser approval/grants, notes/revisions, memory evidence/correction/deletion and responsive mobile behavior remain unverified by this drill. Synthetic pagination and isolation tests use actual SQLite/API behavior and do not establish model quality or third-party integrations. Existing UI plugin badges and the ONLINE indicator are not authoritative integration/readiness evidence. Future native release bundles must be rebuilt from the changed source.

## Notes permissions, revisions and journal

```sh
pnpm --dir frontend build
cd backend
uv run --locked --extra dev python ../scripts/verify-browser-history.py --scenario notes
```

The fixed notes scenario uses the same isolated loopback production API/worker harness and installed Chrome, with an additional explicitly synthetic other-account note. It does not generate a model response; readiness checks the existing local model metadata. No provider is mocked, and this is not new inference/MCP/search evidence.

The browser now provides note edit/cancel, revision history, refresh and accessible controls. Updates send the original expected revision and preserve an idempotency key for retries of the same edit. A stale edit reports conflict and does not replace the current revision. Revoking list clears cached cards/edit/history; revoking history or update clears the corresponding open view. Create network errors report uncertain status so a retry can reuse the original operation key.

The real browser drill checks initial denied list/create, owner-granted note actions, create, edit and revision inspection, actual lost acknowledgment after a committed POST followed by same-key retry, one resulting note and successful-create journal, concurrent authenticated HTTP update followed by stale browser 409, revocation, known other-account note denial, deletion denial/success, preserved deletion revision and visible durable journal entries for denial/conflict/success. Evidence: `docs/evidence/checkpoint-14-browser-notes.json`, screenshot `checkpoint-14-browser-notes.png`, source hashes `checkpoint-14-notes-summary.json`. Concurrent edits use the actual API; acknowledgment loss aborts the real browser route only after the server response completes. Neither substitutes a fake tool result.

This demonstrates note actions through browser controls and registry authorization, not a real-model-selected notes task (older model-tool receipts have their own scope). Memory correction/deletion browser controls, other-account mutation/stream checks and mobile layouts need separate coverage. API/backend tests remain complementary evidence for boundaries not exercised here. Source changes require rebuilding native release bundles.

## Memory evidence, correction, deletion and local inference

```sh
pnpm --dir frontend build
cd backend
uv run --locked --extra dev python ../scripts/verify-browser-history.py --scenario memory
```

The fixed memory scenario uses the same isolated private API/worker, installed Chrome and actual local Ollama digest. It submits synthetic preference messages through the browser; the real idle worker applies the production curated full-message rule grammar. This is deterministic supported-topic learning, not broad semantic/LLM extraction. The browser opens attributable original message evidence, saves a correction with expected revision, inspects correction evidence, toggles retrieval/learning, deletes derived memory and observes topic suppression. The UI refreshes on entering memories/journal; correction selectors reset on revision changes. Checkboxes reflect the requested state while disabled during saving, then reload authoritative settings/epoch before accepting another change.

Fresh-conversation model probes inspect only the explicit `Personalization hints` section: actual output is expected to be CONCISE before correction, DETAILED afterward, NONE with retrieval disabled, and NONE after deletion. Generic base-system style is excluded from the probe because it independently asks for concise responses. The initial ambiguous probe returned CONCISE with retrieval disabled; that failed receipt is preserved as `checkpoint-15-memory-failed-probe.json`. It is not evidence of a passing disabled-retrieval model check. Final receipt/screenshot: `checkpoint-15-browser-memory.{json,png}`; exact source/lock/version hashes: `checkpoint-15-memory-summary.json`.

A development-only read-only `node:sqlite` observer opens only the disposable synthetic database named in the private harness configuration. It waits for the repeated-source learning job to reach done before asserting no deleted-topic regeneration, verifies disabled learning enqueues no job, and checks that the descriptive routine produces no tool operations. No production diagnostic route, fake provider or substituted tool response is introduced. The temporary configuration/database and owned server/browser processes are removed afterward. See the current [Node 24 SQLite API](https://nodejs.org/docs/latest-v24.x/api/sqlite.html) for readOnly/prepare/get; this uses the already installed Node version, with no new dependencies.

Memory control learned/corrected/deleted/settings journal entries must exist and become visible on entering the journal. No page errors or external application requests are permitted. This does not prove general task quality, third-party credentials/MCP/search, real-model transport cancellation or all-account browser isolation. The routine check proves no tool operation in this isolated run; the product still intentionally has no scheduler. Historical chat remains retained when derived memory is deleted. API/backend memory tests cover sensitive/injected content rejection and evidence deletion; the browser scenario does not repeat all those adversarial inputs.

Owner tool grant/revoke/version checks now have a separate actual Chrome receipt: see [tool permissions](tool-permissions.md) and `docs/evidence/checkpoint-23-browser-permissions.json` (25 checks passed). This does not claim MCP execution or credentialed search.

## Live research browser check (2026-11-08)

The isolated `research` scenario in `scripts/verify-browser-history.py` uses a disposable owner, the private configured Tavily key file, actual Chrome, the local worker and Ollama, and one public query. The [receipt](evidence/v1-browser-research-retry-20261108.json) records a successful search tool result, a completed model response, source URLs that matched search results, and the rendered answer. The first [attempt](evidence/v1-browser-research-20261108.json) failed before submitting a query because a UI selector was outdated. The successful run returned SQLite's official WAL documentation and other sources. Its answer correctly identifies checkpointing but simplifies the disk-write comparison; SQLite describes checkpoint and workload tradeoffs in its [WAL documentation](https://www.sqlite.org/wal.html). At the time, URLs rendered as text. This one example is not a general accuracy benchmark, and the research scenario requires the configured credential and may incur a search charge.

The [2026-11-08 Markdown check](evidence/v1-chat-markdown-20261108.json) verifies formatted headings, emphasis, lists, code, tables and clickable source links in an isolated Chrome session. It also checks that unsafe links, raw scripts and remote images do not execute or load. The new native release passed its [startup check](evidence/v1-markdown-native-20261108.json) and was then deployed to the owner-approved user service; the private route returned ready and denied unauthenticated conversation access.

## Practical MVP checks (2026-11-08)

The `mvp` scenario used Chrome, an isolated owner and the actual local model. The [receipt](evidence/v1-mvp-workflows-retry-20261108.json) covers an approved-file answer, creating and viewing a note, journal display, persisted memory setting and permission revocation. The first [receipt](evidence/v1-mvp-workflows-20261108.json) failed a test assertion that compared SQLite's numeric Boolean with a JavaScript Boolean; the setting itself was saved. The [memory retry](evidence/v1-mvp-browser-memory-retry-20261108.json) passed learning, correction, deletion, settings and real-model preference checks. Its [first attempt](evidence/v1-mvp-browser-memory-20261108.json) used an outdated assumption that no tools would be called anywhere in the scenario; the updated check examines the routine request specifically. The older notes-browser script also stopped on outdated UI/default-permission assumptions; the current MVP scenario exercised notes successfully. The running native release's application and frontend files matched those tested. All browser test accounts and content were disposable.
