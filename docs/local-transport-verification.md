# Local inference stream completion and failure verification

The `tests/test_runs.py::test_loopback_provider_outage_and_recovery` test exercises an actual loopback connection refusal through the adapter and worker, then starts a synthetic HTTP provider on the same port. It checks that the run fails with a safe error, resubmitting its operation ID does not retry it, and a new run succeeds after service recovery. The provider response is synthetic; this does not establish behavior after an actual model-server crash or during an interrupted tool write. The test uses an isolated database and does not touch the running model service.

`tests/test_runs.py::test_provider_socket_reset_does_not_retry_and_new_run_recovers` uses a synthetic loopback provider that sends an incomplete structured response and resets its TCP connection. It verifies the actual adapter and worker record a sanitized failure, do not retry the same operation, and can complete a new run after recovery. This covers the initial structured response, not a reset during a generated stream, an actual model-server crash, or a write in progress.

`tests/test_runs.py::test_stream_socket_reset_preserves_persisted_output_and_replays_failure` separately resets an actual loopback TCP stream after synthetic prefixes of 600 characters and a short, still-buffered fragment. It checks the failed run's saved output and terminal events, no retry of the original operation, and a successful new run. The short case freezes the worker's batching clock and observes token consumption to establish that the fragment was not yet persisted. It does not exercise real inference or a tool write.

On an execution exception the worker also attempts to persist a short pending token batch before recording the failure. `test_provider_failure_flushes_short_buffered_output` verifies this with a synthetic failing generator. Normal lease, cancellation and output limits still apply; a process crash, cancellation, or storage failure can lose a batch that has not yet committed.

Local Ollama streams must end with a boolean `done:true` frame. SGLang's OpenAI-compatible stream must end with `[DONE]`. HTTP EOF alone is not a completed answer. Adapters now reject missing completion markers with sanitized errors; Ollama error frames and string-valued false completion markers also fail. Cancellation still propagates through the existing worker lifecycle.

```sh
cd backend
uv run --locked --extra dev pytest -q tests/test_model_stream_completion.py
uv run --locked --extra dev python ../scripts/verify-local-transport.py
```

The six protocol tests are explicitly synthetic HTTP fixtures. They cover completion versus truncated EOF for both adapters, Ollama error frames and invalid completion type. They are not live inference evidence, and no live SGLang claim is made.

The actual integration verifier uses the existing local Ollama `gemma4:26b` digest pinned in the script. It starts only its own loopback FastAPI forwarding relay and temporary synthetic SQLite owner. The actual worker runs in-process under the real worker lock. The model selects a permitted note creation; the note operation commits through the real registry. The relay forwards real Ollama NDJSON, then closes its own response after a real nonempty content frame, withholding the completion marker. No model result, token, tool selection or tool outcome is invented. No existing Ollama process/configuration is stopped or altered.

The run must fail with saved partial output and sanitized error, while the confirmed note operation and journal remain successful. A failed final answer does not undo an already confirmed note. An authenticated ASGI SSE request after the last saved token must replay terminal failure without duplicating tokens; canonical history must retain failed partial output. This is actual API routing/auth/SQLite behavior in-process, not a browser or separate HTTP app server. Reusing the client operation ID must return the failed run without another generation or note; a subsequent worker claim must find no retry. Generation counters cover forwarded chat requests, not model metadata checks.

The fault is then disabled and a new real streamed response must include the actual completion frame and return exactly TRANSPORT_RECOVERED. The verifier has a 240-second overall deadline, bounded upstream requests and owned relay teardown. Private synthetic data is removed. Evidence starts passed:false/in_progress; errors write sanitized error type, and interruption remains visibly incomplete. Receipts contain no owner secrets, provider bodies or user content. The recorded partial output is hashed; all prompts/note data are synthetic.

Evidence: `docs/evidence/checkpoint-16-local-transport.json`; source/lock/version hashes `checkpoint-16-transport-summary.json`. An earlier verifier status assertion incorrectly expected completed instead of the registry's succeeded; its failed receipt is preserved as `checkpoint-16-verifier-status-failure.json`. That was a verifier failure, not a passing integration.

Current [Ollama streaming documentation](https://docs.ollama.com/api/streaming) describes NDJSON final done:true; the [chat API](https://docs.ollama.com/api/chat) defines its boolean completion field. Existing dependency/model choices are unchanged. Current mutable model-page prefixes differ from the cached digest; the drill does not establish latest-model or complete immutable runtime/GPU provenance.

This proves deliberate truncation of the relay's real forwarded response after a confirmed tool write, durable run failure/replay, and normal subsequent streaming. It does not prove upstream GPU inference stops when a client disconnects, real-provider latency timeout, a socket reset during real inference, model-server process crash, host/storage outage, worker restart during a write, unknown write recovery or live SGLang compatibility. Those remain separate gates. Cloud inference stays disabled and all services stay private. Rebuild native release bundles before deploying changed adapter code.

## Active-response timeout and cancellation

```sh
cd backend
uv run --locked --extra dev python ../scripts/verify-local-transport.py --scenario timeout
uv run --locked --extra dev python ../scripts/verify-local-transport.py --scenario cancel
```

These fixed development scenarios forward an actual model-selected note write and a real Ollama content prefix, then pause the relay's response for 90 seconds. The timeout scenario uses the existing worker's configured 10-second whole-run budget; it must finish failed with Run time limit exceeded. This simulates a delayed response body with real generated data; it is not an induced slow GPU or the adapter's 60-second HTTP read timeout. The cancellation scenario waits for a persisted real prefix, sends an authenticated cancellation through the actual ASGI route, and requires canceled with no fabricated provider error.

Both require HTTPX's upstream response/client closed flags in the owned relay after the app disconnects, preserved partial output/history/SSE terminal replay, exactly one confirmed note and accurate success journal, no automatic/idempotent generation retry, and successful subsequent real streaming. Worker and API routing run in-process; there is no separate application server/browser in this drill. Metadata checks and generation use the existing local endpoint only. Receipts record terminal status/error and measured whole-run elapsed time; these are drill timings, not a quality or latency benchmark.

Receipts: `docs/evidence/checkpoint-17-local-timeout.json`, `checkpoint-17-local-cancel.json`; source/version hashes: `checkpoint-17-interruption-summary.json`. Cleanup proves observed HTTPX object closure, not GPU scheduling or compute termination inside Ollama. Model-server process failure, a socket reset during real inference, adapter read-timeout, worker SIGKILL during a write and host/storage outages remain untested by these scenarios. The default truncate scenario remains available; checkpoint-16 receipts refer to their recorded earlier source hashes. No provider/model/dependency or production configuration is changed by running the isolated drill.
