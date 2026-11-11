# Local model provenance and bounded observations

This is a partial installed-runtime fingerprint and a small real-generation observation, not a full model release approval or broad benchmark. It keeps the existing local Ollama service/model and does not pull, upgrade, unload, stop a service, or alter drivers/networking. No cloud inference or private user conversation is used.

## Reproduce

Use the [fixed SQLite wrapper](sqlite-runtime.md). From repository root:

```sh
archer_uv run --project backend --locked --extra dev python scripts/verify-local-model.py
archer_uv run --project backend --locked --extra dev python scripts/verify-local-model.py --planning-follow-up
```

The collector reads metadata from the fixed loopback endpoint, hashes at most 3 GiB of readable installed engine libraries, captures host counters, and makes four sequential synthetic requests. It has a six-minute asyncio budget and bounded per-request/output/frame limits. Synchronous local-file hashing is byte-bounded and cannot be preempted by that asyncio deadline until control returns; it is not a hard storage-stall timeout. It refuses generation if another model is loaded at its preflight check; it does not evict a workload. That check is not a cross-client inference lock. Existing host activity can affect results. Follow-up uses the original planning prompt and a larger prediction cap; its receipt is separate, preserving the initial failure. Output filenames are checkpoint-25 receipts and rerunning refreshes them; copy evidence elsewhere first if retaining a historical run.

## What is bound and what remains missing

The local API reports **Ollama0.34.2**, cached **gemma4:26b** digest `08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68`, 25.2B Q4_K_M, artifact size **18,604,148,513 bytes**, completion/tools/thinking/vision. The current [public mutable model tag](https://ollama.com/library/gemma4:26b) differs, so no current-tag identity/equivalence claim is made and no update was pulled. Keep the reviewed cached digest until a separately measured recipe is approved.

`/api/show` verbose metadata captures scalar model/tokenizer information and canonical JSON SHA256 for all array fields. Tokenizer tokens/scores/types each contain **262,144** entries; merges contain **514,906**. Array hashing uses JSON sorted keys, compact separators and ensure_ascii=True. The complete API model_info hash is `d7ed786d55cb5dd46297e7a26a8cf311939193cb1daaa541d7859c2269a29a7d`. This fingerprints an API snapshot, not independently read GGUF bytes.

The returned template is only `{{ .Prompt }}` (13 bytes), SHA256 `b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315`. Effective chat/tool formatting also depends on the engine's gemma4 renderer/parser. The recipe identifies three blob references, a draft model with draft_num_predict3, template/renderer/parser, and sampling parameters. The collector records those references; it does not hash protected weight files successfully.

Readable installed `/usr/local/bin/ollama` is **26,050,600 bytes**, SHA256 `e5b6c340875587839c10e88645a1fca66c0618cd2796ece748859f4efd940a65`, stable while hashed. **27** installed library/executable files totaling **2,211,785,344 bytes** are also fingerprinted, including CPU/CUDA variants. This inventory is not a list of libraries actually mapped by the inaccessible running process. Binary Go metadata reports Go1.26.0, module devel and no VCS revision. [Official version source](https://github.com/ollama/ollama/tree/v0.34.2) resolved to immutable commit `dfabde4539e42ba1e1eab50a3a50b88aea7958a0`; renderer/parser/go.mod source hashes and immutable URLs are in checkpoint-25-official-runtime-source.json. No source-to-installed-binary binding or publisher-authentication proof is implied.

Direct read access to the selected model manifest, blob paths and `/proc/<serving-pid>/exe` returned **PermissionError**. On 2026-11-08 the owner supplied [output from the read-only verifier](evidence/v1-model-owner-check-20261108.json): the manifest and all three expected blobs matched their expected SHA256 hashes, and the running service executable matched the previously fingerprinted installed binary. This is owner-reported output, not a run witnessed by the assistant. It verifies the selected file bytes at that time, but does not independently establish publisher authenticity, mapped libraries/CUDA backend, or build provenance. The verifier checks the expected blob paths; it does not parse the manifest to prove its references or record the model runner's mapped libraries. Do not change permissions or dump process environments to obtain further evidence.

## Exact bounded generation results

Requests used context4096, temperature0.7, stream=True and explicit num_predict256, with the model's existing thinking/draft defaults. These are direct engine probes, not full Archer worker/UI latency. No thinking text was stored; the larger follow-up records only the count of thinking characters.

| Case | Wall seconds | First visible content seconds | Outcome |
| --- | ---: | ---: | --- |
| Initial sentinel, no model listed before run | 8.757 | 8.693 | Exact sentinel; load_duration7.020s |
| Repeated warm sentinel | 0.657 | 0.625 | Exact sentinel; prompt cache22/27 tokens |
| Errand arithmetic, prediction cap256 | 2.395 | none | done_reason=length, empty visible answer; **failed** |
| Source extraction | 1.478 | 1.478 | Correct: Library |
| Same errand prompt, cap1024 follow-up | 5.854 | 5.727 | total_minutes=85; end_time=10:25; stop |

Initial quality is **3/4**, not 4/4. The same-prompt follow-up at 17:27:12 UTC passed with643 reported generated tokens and1,444 observed thinking characters. It demonstrates that this selected tight cap can exhaust before a useful answer, not general planning competence. Default Archer requests do not set this benchmark prediction cap. No automatic thinking-mode change or production prediction cap was introduced. The default/example context was reduced from unmeasured262144 to the previously tested4096-token native recipe; existing explicit operator overrides are untouched and larger contexts remain unvalidated. Model maximum context is not measured safe resource capacity.

Reported generation rates in the initial probes are roughly94–113 server-reported tokens/s. They include thinking and may reflect speculative-decoding server accounting and are **not user-visible text throughput**: for example, short sentinel output reports145 generated tokens. Four synthetic cases, one warm repetition and one follow-up do not establish median/p95, concurrency throughput, real-user quality, all tools, or absence of hallucinations.

## Resource scope and discrepancy

Host snapshot: Ubuntu24.04.4, kernel6.17.0-1031-nvidia, ARM64, NVIDIA GB10 compute capability12.1, driver580.173.02, system memory130,663,170,048bytes. Driver module information, GPU UUID, installed-byte hashes and exact counters are in the receipt. Listener observed on127.0.0.1:11434. This fingerprints observed host state, not an immutable operating-system image or driver build verification.

There were45 resource samples. GPU utilization rose from0% to high values during generation, with observed power/temperature changes. Available system memory fell from122,493,988,864 to a sampled minimum100,407,590,912bytes. These are host-wide counters with collector/workload/page-cache effects, not isolated model RSS or GPU memory attribution. Dedicated GPU memory totals/usage report N/A here.

After generation `/api/ps` reports **986,154,269 bytes** for both size and size_vram at4096 context, far smaller than the18.6GB model artifact. Their equality does **not** prove full model GPU residency. The active CUDA backend and actual mapped allocation require additional accessible process/backend evidence. The collector leaves normal model keep-alive behavior in place and sends no unload request.

Current official [chat API](https://docs.ollama.com/api/chat), [running model metadata](https://docs.ollama.com/api/ps), and [usage metrics](https://github.com/ollama/ollama/blob/v0.34.2/docs/api.md) were checked through find-docs and primary sources; durations are nanoseconds, and first-content timing is separately measured by the client. Initial show-document URL guesses returned404/unavailable; official source/API documentation supplied the reference. No model/runtime version was proposed as a replacement.

Receipts: checkpoint-25-local-model.json, checkpoint-25-planning-follow-up.json, checkpoint-25-official-runtime-source.json. Initial collector failure (binary readelf text was not UTF8) is preserved in checkpoint-25-local-model-initial-failed.json; replacement decoding fixed collection. Full backend181 tests passed28.21s after the default change. This does not clear full provenance, resource-quality-latency or remaining V1 acceptance gates.
