# Archer: Research and Implementation Plan

## Executive recommendation

**Build Archer in three phases: V1 Safety, V2 Target, and V3 Reach. Start with V1.**

Archer is a local-first assistant that runs on the owner's NVIDIA DGX Spark, remembers useful preferences, and connects to approved tools through plugins. Its longer-term direction is an autonomous computing layer spanning workstations, smart-home devices, manufacturing, voice, and spatial interfaces.

V1 is a complete, useful product on its own: an assistant with a Codex-inspired chat and action interface, served from the house and reachable through a public HTTPS URL with private accounts. V1 does not require an editor, terminal, coding agent, voice system, hardware controller, native app, or webcam HUD.

The releases are:

- **V1 — Safety:** secure remote access, local inference, private workspaces, files/web/notes plugins, automatic preference learning, suggested routines, and a visible action journal.
- **V2 — Target:** gaming and productivity workflows, workstation and home automation, manufacturing, reliable voice, desktop/mobile apps, and explicitly authorized event-driven routines.
- **V3 — Reach:** a Linux-based Archer OS, webcam context and gestures, phone LiDAR experiments, and optional wearable adapters.

Progress is measured by completion gates. There is no committed calendar deadline. Each release must remain usable without the next phase.

> **V1 succeeds when an invited user outside the house can securely use Archer, complete a plugin-assisted task with local inference, see what happened, and return to their own conversations, notes, and learned preferences.**

### Confirmed decisions and prerequisites

The DGX Spark is available. Model inference defaults to that machine; cloud inference is optional, explicitly enabled, and identified in the interface. Each invited account has private conversations, notes, memories, and approved file access. The owner manages plugin configuration; public registration and a plugin marketplace are outside V1.

Cloudflare Tunnel is the default route to the public URL. Brave Search API is the default search provider. Their account setup, domain configuration, credentials, and any applicable service costs are deployment prerequisites. Optional cloud inference also requires provider configuration and credentials.

Local-first describes where Archer runs and stores its state. HTTPS traffic passes through the tunnel provider; search queries reach the search provider; cloud-enabled requests reach the selected model provider. Remote access depends on the home system's power and Internet connection. A cloud model does not keep the website available if Spark itself is offline.

This document is a specification for future application work. Architecture, example workflows, and proposed test targets are not claims that the application or integrations already exist.

## Shared architecture and phase boundaries

### Archer Core owns application behavior

Use a Python/FastAPI backend, React/TypeScript frontend, SQLite persistence, and a bounded background worker for runs and memory extraction. Separate model generation from authorization, tool dispatch, persistence, and observable outcomes.

```text
V1 browser: desktop or phone
             |
      Public HTTPS hostname
             |
       Cloudflare Tunnel
             |
       Archer web gateway
       - authentication
       - account isolation
       - REST + event stream
             |
         Archer Core
       - assistant runs
       - plugin permissions
       - preference memory
       - notes and history
       - action journal
             |
       +-----+------------------+
       |                        |
Local model endpoint      Approved plugins
on DGX Spark              - read local files
                          - search the web
Optional cloud adapter    - personal notes
with explicit opt-in      - reviewed MCP servers

V2 extends Core with device state, capabilities, voice,
workstation nodes, manufacturing, and event-driven routines.

V3 adds Archer OS and spatial clients using the same Core.
```

A logical service decomposition is:

```text
archer-core/
  gateway/       accounts, sessions, API, event streaming
  cognition/     model adapters, run loop, result verification
  plugins/       registry, schemas, dispatch, credential references
  permissions/   grants, approvals, resource constraints
  memory/        preference extraction, provenance, corrections
  storage/       conversations, notes, durable run state
  journal/       tool calls, outcomes, errors
  worker/        bounded execution and background extraction

V2 additions:
  world/         entities, device state, capability graph
  scheduler/     approved routines and event triggers
  workflows/     coordinated multi-system execution
```

These are subsystem boundaries, not a requirement to create all directories before the first vertical slice works. V1 needs a tool registry and durable run state; it does not need the full world model or scheduler.

### Stable interfaces

All clients use the same authenticated Archer API. V1 defines interfaces for accounts, conversations, runs, streaming events, cancellation, notes, memory corrections, plugin settings, and tool approvals. Each account-owned object carries an owner identifier, and authorization is checked on every request and tool invocation.

Model adapters accept conversation context and permitted tool schemas, produce streamed output or structured tool requests, and support cancellation. Vision, embeddings, speech, and specialized model routing are later extensions rather than mandatory V1 adapter methods.

The run loop is:

```text
Authenticated request
  -> owned conversation and permitted context
  -> selected provider
  -> proposed tool call
  -> server-side validation and permission check
  -> approval when policy requires it
  -> execution with limits
  -> persisted outcome and event stream
  -> response and background memory extraction
```

Use Server-Sent Events for the V1 browser action stream, with persisted event identifiers for reconnects. Disconnecting a browser does not silently start a second run. FastAPI documents SSE and event-ID handling in its [streaming guide](https://fastapi.tiangolo.com/tutorial/server-sent-events/).

A journal entry records the account, conversation, run, tool, sanitized arguments, permission decision, timestamps, and result or error. An accepted tool request is distinct from a verified outcome. The UI must show partial completion and failures accurately.

## V1 — Safety: secure local assistant

### Product experience

Build a responsive web interface containing:

- Streaming chat, saved conversation history, and a clear indicator of the provider used for each run.
- A live action timeline showing tool requests, approvals, progress, results, and failures.
- Cancellation and reconnect behavior that preserves the current run's state.
- Personal notes with read, create, edit, and delete controls.
- A memory view explaining what Archer learned, its supporting evidence, and how to correct or delete it.
- Suggested routines that are inspectable but cannot schedule or execute themselves.
- Plugin status and permissions; owner-only administration for configuration.

A phone browser is supported in V1. A packaged desktop application and a native mobile application belong to V2.

### Accounts and isolation

The owner creates or invites private accounts; there is no anonymous use or public signup. Use established password hashing and secure server-side sessions with expiration and revocation. Protect browser writes against cross-site request forgery, validate origins, throttle login attempts, and enforce per-user request and resource limits.

Every retrieval path must enforce account ownership: conversation history, run events, notes, memory records, approved folders, and plugin configuration. Knowing another user's object identifier must not provide access.

The owner administers service configuration and grants. Ordinary users cannot install servers, change model endpoints, expand file roots, or access other users' credentials. The administration interface must not automatically expose other users' conversation content. The machine's system administrator still has physical access to local storage; account isolation is an application boundary.

### Local inference and optional cloud models

Serve the local model on an internal OpenAI-compatible endpoint. Begin with a documented tool-calling recipe for one DGX Spark, then validate model, container architecture, inference engine, precision, parsers, and memory settings together on the actual machine. NVIDIA's [Spark recipe guide](https://build.nvidia.com/spark/vllm/choose-recipe) makes these compatibility requirements explicit; its [playbook repository](https://github.com/NVIDIA/dgx-spark-playbooks) provides deployment starting points.

Do not select a model solely from its parameter count or assume Spark can run every specialty model concurrently. Benchmark the initial candidate for correct tool selection, structured arguments, task completion, failure recovery, memory adherence, latency, and resource use. Pin the tested model revision, container digest, and launch configuration in deployment documentation.

Support an optional cloud endpoint through the provider boundary. Credentials stay server-side. Show cloud configuration and consent clearly; do not silently forward local conversations to a cloud provider because a local request failed. Background memory extraction follows the same provider and consent rules.

Cloud requests are bounded by configured usage limits. A provider timeout, missing key, or exhausted budget becomes an actionable error rather than an unbounded retry loop.

### Starter plugins

**Approved files:** list, search, and read files within owner-approved roots assigned to that account. Start with documented supported text formats and explicit size limits. Resolve canonical paths, reject traversal, and prevent symlink escapes. This plugin cannot edit project files, execute commands, or browse the entire host filesystem.

**Web search:** use Brave Search API and return source titles, URLs, and relevant excerpts. The assistant cites the actual sources used and distinguishes retrieved information from inference. The [Brave API documentation](https://api-dashboard.search.brave.com/documentation/services/web-search) describes the search endpoint and credential requirements. If credentials are missing, report that search is unavailable; do not fabricate search results.

If V1 fetches result pages, the retrieval boundary allows only public HTTP/HTTPS destinations. Reject local, private, loopback, link-local, and metadata-service addresses, including redirects and DNS-resolution changes. Enforce response size, content-type, and time limits. Search and fetched page content remain untrusted input.

**Personal notes:** read, create, update, and delete notes owned by the account. Keep these separate from approved source folders. Note writes follow configured permissions and explicit user intent; approval-required actions expose the concrete change before execution. Record outcomes and use operation identifiers to prevent duplicate writes.

### Reviewed MCP extensibility

Use MCP as the external connector boundary. MCP defines schema-based tools and contextual resources; see the [tool specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).

The owner can configure reviewed servers through configuration files and a connector guide. The registry records transport, credential references, allowed tools, account grants, scopes, and timeouts. New configuration is validated before activation. Fail closed when a tool is unknown, disabled, or outside a grant.

Archer enforces permissions independently of the model and server-provided descriptions. MCP annotations are hints, not guarantees or authorization; the protocol maintainers explain this distinction in their [tool-annotation guidance](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/).

A shared external server must not accidentally share account data or credentials. Use isolated account connections where necessary, or deny the integration until isolation is demonstrated. A tool that asks for broader authority does not receive it from the assistant's judgment or learned memory.

V1 includes no unrestricted shell, arbitrary code execution, coding workspace, plugin marketplace, or user-supplied server installation. Third-party tools remain constrained by the reviewed registry and grants.

### Automatic learning with provenance

V1 learns useful, non-sensitive preferences from the user's own conversations and corrections. It also identifies reusable routines and saves them as suggestions. It does not turn learned behavior into standing permission or a scheduled action.

The memory cycle is:

```text
User conversation or correction
  -> bounded extraction job
  -> preference candidate and source evidence
  -> sensitivity and ownership checks
  -> merge with existing memory
  -> retrievable preference or routine suggestion
  -> user correction/deletion
```

Keep conversation history, notes, and learned memories distinct. History is the record of the chat; notes are managed content; memory is a compact, inspectable interpretation that may be wrong.

A memory record needs account ownership, type, content, supporting message references, origin (inferred or corrected), timestamps, and validity. Confidence may help retrieval but cannot override an explicit correction. Conflicting or weak evidence should remain qualified rather than become an unquestioned fact.

Provide controls to view evidence, correct or delete individual items, and disable learning. Deleted or corrected memories must not immediately reappear from old extraction jobs. Exclude credentials and sensitive inferred traits; do not learn personal preferences from instructions embedded in web pages or plugin output.

V1 routine suggestions are descriptive. Executable procedures, device-state learning, and event-driven autonomy are V2 features.

### Run safety, persistence, and recovery

Validate tool names and arguments before dispatch, and check current permissions again at execution. Apply tool timeouts, bounded run length, worker queue limits, output limits, and secret redaction. A denied action appears in the journal with a useful reason.

Retrieved documents and server instructions cannot change system policy, account identity, or tool grants. Network and filesystem controls provide the enforceable boundaries; prompting alone is insufficient.

Persist conversations, notes, memories, run state, approvals, and tool outcomes. On restart, identify interrupted runs and reconcile known results. Do not blindly replay writes or report success when execution status is unknown. Retries depend on verified operation semantics and idempotency.

Cancellation stops further work and requests termination of active operations where supported. Explain operations that have already completed or cannot be canceled. Expired approvals, revoked grants, and deleted accounts cannot authorize later execution.

Use consistent SQLite backups that account for active database state, then verify restoration to a clean instance. Keep credential recovery separate from application-data backups and restrict access to both.

### Deployment and remote access

Run the application and model services locally on Spark. Bind internal services to private interfaces or container networks. Publish only the web gateway through a Cloudflare Tunnel hostname. Cloudflare documents this public-hostname-to-local-service route in its [routing guide](https://developers.cloudflare.com/tunnel/concepts/routing/); its [configuration guide](https://developers.cloudflare.com/tunnel/configuration/) describes outbound tunnel connections.

Provide deployment configuration, protected server-side secrets, database volumes, health checks, bounded logs, startup recovery, and backup/restore instructions. Pin tested dependencies and container images. Confirm ARM/GB10 compatibility on Spark rather than treating a successful development-machine build as deployment proof.

Document domain, DNS, tunnel, search-key, and optional cloud-key setup. Never put credentials in the frontend bundle, repository, screenshots, or action journal. Private responses and event streams must not be cached by the public gateway.

### V1 build sequence and gates

1. **Local chat vertical slice:** run the browser, Core, and local model together; demonstrate streaming output, cancellation, provider identity, and saved history.
2. **Private accounts:** add owner-created users, secure sessions, ownership checks, and isolation tests before opening remote access.
3. **Starter plugins and journal:** demonstrate file reading, cited web search, personal notes, enforced permissions, and visible outcomes.
4. **Automatic memory:** demonstrate useful preference learning, source evidence, explicit corrections, deletion, and non-executable routine suggestions.
5. **Reviewed MCP configuration:** connect one reviewed test server, document setup, and verify grants, isolation, timeouts, and disabled-tool handling.
6. **Public access and release verification:** publish the gateway, test from a genuinely external network, exercise failure cases, and verify restart and restore behavior.

Advance only when the previous gate works end to end. V2 work does not substitute for an unfinished V1 gate.

### V1 acceptance criteria

**External use:** authenticate over cellular or another external network, complete a plugin-assisted request with local inference, and return to saved state after reconnecting. A public URL alone is not evidence of successful remote operation.

**Isolation:** account A cannot read, mutate, stream, or invoke tools against account B's conversations, runs, notes, memories, file roots, or credentials. Include direct API attempts with known identifiers and isolation checks for reviewed MCP connections.

**Useful tasks:** summarize a permitted file; research a topic with source citations; create, edit, retrieve, and delete a personal note. Missing files, unsupported content, unavailable search, and denied permissions produce clear errors.

**Learning:** infer a useful preference with attributable evidence, apply it to a later chat, accept a correction over the inference, delete it without immediate regeneration, and save a routine suggestion without scheduling or executing it. Sensitive information and injected tool instructions must not become preferences.

**Boundary protection:** test traversal and symlink escape, unknown tools, malformed arguments, revoked grants, malicious retrieved instructions, private-network URLs and redirects, expired sessions, login throttling, browser write protection, and secret redaction.

**Reliability:** exercise model/plugin timeouts, unavailable providers, cancellation, stream reconnects, concurrent accounts, bounded queues, restarts during a write, and backup restoration. Confirm that uncertain outcomes remain uncertain and completed writes are not duplicated.

**Cloud consent:** verify that local runs send no inference requests to the configured cloud provider; cloud runs require opt-in, respect budgets, and identify the provider. Include background learning in these checks.

Record actual results, model/container revisions, resource use, and known limitations. Security-boundary failures block V1 release. Benchmark numbers and repeated-run counts are evidence to collect, not results assumed by this document.

### V1 release demonstration

From outside the house, open Archer's HTTPS URL and sign in. Ask it to summarize a permitted local document, research a related question, and save the result as a note. Show source links and the action timeline.

Demonstrate a learned preference in a later conversation, open its evidence, correct it, and show the correction taking effect. Show a suggested routine without executing it. Briefly demonstrate another account's separate workspace and an unavailable or denied plugin action.

This is the committed V1 story. It requires no thermostat, voice wake word, printer, native app, or webcam effect.

## V2 — Target: workflows, hardware, voice, and apps

### Hardware discovery comes first

The earlier research listed Raspberry Pi 5/3B/Zero W devices, Pico W boards, ESP-WROOM-32 boards, two gaming PCs, a NAS/home server, Nest thermostats, Govee plugs, a Bambu Lab P2S, and microphones/speakers. Treat this as a reported inventory to confirm before making device-specific commitments. DGX Spark availability is confirmed for V1.

For each V2 device, record the exact model, firmware, operating system, location, supported integration, authentication method, permitted operations, and recovery behavior. Verify connectivity and control on the actual hardware before adding it to a workflow.

Proposed roles remain useful: Pi 5 as a Home Assistant gateway; other Pi devices as optional room endpoints; ESP/Pico boards as sensors, buttons, or indicators; PCs as workstation targets; NAS as storage or backup; printer as a manufacturing endpoint. Custom peripherals are optional hardware experiments, not prerequisites for every workflow.

### Gaming and productivity workflows

Install an authenticated Archer Node on supported workstations. Prefer explicit actions over vision-driven desktop clicking. The node connects back to Core, reports status, and executes granted operations such as launching an app, opening a project or URL, adjusting volume, or sleeping the machine.

A proposed node interface is:

```text
pc.get_status
pc.wake
pc.launch_app
pc.open_project
pc.open_file
pc.open_url
pc.run_allowed_command
pc.set_volume
pc.sleep
pc.shutdown
```

Commands require explicit allowlists and OS-specific implementations. Wake-on-LAN is available only after verifying firmware/NIC support. Shutdown uses the node's controlled operating-system action rather than cutting mains power.

A workspace routine can restore the user's game or project, approved applications, documentation, and relevant state. The planner selects from capabilities and current state; deterministic executors verify outcomes. Already-running applications and unavailable machines must be handled without repeating harmful actions.

### Smart-home and arrival workflows

Use Home Assistant as the proposed device abstraction instead of spreading vendor-specific control through Core. Its documented [REST API](https://developers.home-assistant.io/docs/api/rest/) provides authenticated requests, and its [WebSocket API](https://developers.home-assistant.io/docs/api/websocket/) supports event subscriptions.

Nest and Govee support must be verified for the actual devices, account permissions, and chosen integrations. Do not assume every model exposes the same functions.

The preserved arrival workflow is:

```text
"I'm heading home. Prepare my workspace."
  -> resolve authorized location and routine
  -> recall relevant confirmed preferences
  -> set supported thermostat within allowed range
  -> turn on approved desk equipment
  -> wake the workstation when supported
  -> wait for node connectivity
  -> restore the project or gaming workspace
  -> verify outcomes and journal the episode
```

Begin with an explicit typed or spoken trigger. Geofencing is optional until location reliability and consent have been tested. Partial failures must remain visible; an offline plug should not cause the assistant to claim the whole house is ready.

### Capability graph and standing permissions

Extend the V1 registry with entities, capabilities, current state, preconditions, reversibility, verified idempotence, expected duration, and constraints. Capabilities are attached to owned devices and locations rather than exposed as an unstructured list of functions.

```text
Account -> authorized location
             -> thermostat -> permitted setpoint range
             -> workstation -> wake / restore workspace
             -> printer -> status / prepare / authorized job action
```

Standing permissions allow approved routines to proceed without repeated confirmation. Device limits, destructive actions, external communication, and spending constraints remain enforceable outside the model. Learned preferences cannot expand grants.

An illustrative policy might permit waking a named workstation and setting a named thermostat within an approved range, while denying file deletion and account changes. Printing needs separate physical-operation authorization and material/runtime limits established for the actual setup.

Add event triggers and scheduling only for explicitly enabled routines. Journal triggers, plans, permission decisions, actions, verified state changes, and recovery attempts.

### Richer memory and outcome learning

Preserve the original four-layer design as V2's expansion:

- **Working:** the active objective, pending steps, and recent observations.
- **Episodic:** what happened in a workflow and how the user corrected it.
- **Semantic:** stable facts and preferences with provenance.
- **Procedural:** reviewed, executable routines with outcomes and permissions.

The research basis includes [MemGPT](https://arxiv.org/abs/2310.08560), which studies memory tiers beyond a fixed context window, and [CoALA](https://arxiv.org/abs/2309.02427), which organizes language agents around memory, action spaces, and decision-making. These are design references, not mandatory runtime dependencies.

Consolidate useful episodes into preferences or proposed routine improvements. Keep corrections, conflicting evidence, and invalidated memories inspectable. A successful workflow can improve a procedure only within its existing authorization.

### Voice agent

Build a local voice pipeline using candidate wake-word, voice activity detection, speech-recognition, and speech-synthesis components. The earlier candidates were openWakeWord, Silero VAD, Whisper, and Kokoro. Recheck licensing, model availability, Spark/ARM compatibility, and deployment behavior before selection.

```text
Microphone -> wake word or push-to-talk -> speech boundaries
  -> transcription -> Archer Core -> authorized actions
  -> response synthesis -> speaker
```

Start with push-to-talk to validate the whole loop. Add "Hey Archer" after wake-word performance is measured. Required behavior includes interruption, cancellation, feedback about listening/thinking/speaking, ambiguous-command handling, and recovery from missing audio devices or noisy environments.

Use the same conversation, permissions, and journal as typed requests. Voice does not create a separate authority channel.

### Desktop and mobile apps

Add packaged desktop and native mobile access over the same Core API. Preserve account identity, conversations, notes, learned preferences, active runs, and notifications across clients. Use shared UI and protocol components where practical, while allowing platform-specific audio and device features.

Choose packaging frameworks and supported platforms at the V2 app design gate, after checking current platform constraints. Do not force V1 to maintain multiple applications in anticipation of this work. A CLI remains an optional client, not a release requirement.

### Manufacturing pipeline

For dimensioned functional parts, use a proposed parametric CAD path:

```text
Natural-language request
  -> explicit dimensional specification
  -> generated CadQuery program
  -> isolated execution
  -> geometry and dimension checks
  -> preview and STL/3MF artifact
  -> validated printer/process/filament profile
  -> slicing
  -> print-ready file
  -> separately authorized transfer/start when supported
  -> monitoring and recorded outcome
```

CadQuery documents export formats including STL, STEP, and 3MF in its [import/export guide](https://cadquery.readthedocs.io/en/latest/importexport.html). Bambu Studio documents slicing and export options in its [CLI guide](https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage). These establish a programmatic design-and-slice route; they do not prove automatic job submission or physical print start on the team's printer.

Preserve the bracket example: specify width, depth, hole diameter, spacing, units, and any missing structural dimensions before generation. Execute generated code without host credentials, uncontrolled network access, or access to the user's files. Validate dimensions, geometry, printer volume, and resource use; show a preview and report invalid designs.

Gate manufacturing in stages:

1. Generate and validate a dimensioned model and print-ready file.
2. Verify supported transfer or queueing on the actual printer.
3. Enable physical start only after verifying the supported pathway, operating conditions, and explicit permission.

A manually confirmed print is a valid demonstration of the first stage. Clearly label manual steps and do not portray slicing as proof that a print completed.

Hunyuan3D remains an optional research candidate for aesthetic meshes. Verify its official release, licensing, resource needs, and geometry limitations before use. Exact functional dimensions stay on the parametric route.

### V2 build gates and completion criteria

Build and verify one workstation workflow, one home workflow, and one manufacturing workflow before coordinating them. Then add richer memory and authorized routines, reliable voice, and the desktop/mobile clients. Hardware discovery and compatibility checks precede each connector.

V2 is complete when the agreed workflows run on verified devices, voice and apps access the same state, and unavailable components produce accurate partial outcomes. Test already-on PCs, offline thermostats/plugs, duplicated tool requests, busy printers, invalid CAD, stale preferences, speech ambiguity, and restarts mid-plan.

Record repeated-run results and actual failure recovery. Target zero unauthorized actions and zero duplicate destructive or physical operations. Task-completion rates must be measured, with failures and manual interventions disclosed.

### V2 release demonstration

Tell a continuous story: from outside the house, ask Archer to prepare an authorized environment and restore a workspace; show verified device changes and workstation state. On arrival, ask what was happening last time and demonstrate relevant memory.

Then request a dimensioned part, show the model and validation, produce a sliced artifact, and demonstrate the verified printing pathway. A labeled timelapse can show the physical result. Finish with a recorded outcome and a reusable, authorized procedure.

Webcam gestures and spatial overlays are not V2 completion requirements.

## V3 — Reach: Archer OS and spatial interaction

### Linux-based Archer OS

Explore a Linux distribution or system shell centered on Archer. Reuse Core, accounts, plugin contracts, and application state. Archer OS should add system integration, service management, device provisioning, useful interface behavior, and measured performance improvements.

Begin with services and a shell on a supported Linux base. Before distributing an OS image, verify drivers, updates, credential storage, boot recovery, installation, backup, and rollback. Compare launch latency, resource consumption, and workflow completion against the V2 baseline. A visual rebranding alone does not demonstrate optimization.

A new kernel or operating system built from scratch is outside the chosen direction. Spark's existing supported environment remains the deployment baseline until an alternative is proven compatible.

### Webcam context and gestures

Prototype a camera view with live action overlays, deliberate gestures, and contextual object selection. Google's [MediaPipe Gesture Recognizer guide](https://ai.google.dev/edge/mediapipe/solutions/vision/gesture_recognizer/web_js) documents web gesture recognition and hand landmarks; it also notes that synchronous processing can block the UI and recommends workers where needed.

Preserve the original interface ideas as experiments: pinch to select, swipe to switch panes, an open palm to display status, and pointing to request context. Measure gesture reliability, latency, and false activations before mapping gestures to actions.

Resolve camera observations against known devices only when evidence is sufficient. "What's happening with that?" should either identify a supported object and retrieve its verified state or ask for clarification. A visual guess cannot authorize device control.

### Phone LiDAR and spatial context

Prototype depth capture and object anchoring on compatible phones. Apple's [ARKit scene-depth documentation](https://developer.apple.com/documentation/arkit/arframe/scenedepth?language=objc) describes LiDAR-based depth and runtime support checks. Not every phone has LiDAR, and a browser camera interface is not assumed to expose native depth data.

Use a native mobile adapter where required. Test depth confidence, lighting, motion, alignment, and device support. Keep a camera-only fallback that provides useful visual context without claiming equivalent depth accuracy.

Camera and depth capture require explicit controls, visible active-capture state, and retention settings. Start with transient processing; retaining scenes or images needs a deliberate product decision and permission.

### Optional wearable adapters and V3 gate

Glasses and wearables are optional clients. The original Meta/Even ideas remain research candidates pending current SDK availability, hardware access, data permissions, licensing, and transport validation. They are not dependencies for the OS or spatial prototypes.

V3 is complete when a working Linux-based Archer environment and camera/depth prototypes demonstrate measured benefits over V2, including graceful operation without supported spatial hardware. Test incorrect object association, failed depth capture, camera permission denial, gesture false positives, offline clients, and OS rollback.

V1 and V2 continue to work without Archer OS, cameras, LiDAR, or wearables.

## Repository organization and collaboration

Grow the repository by phase instead of creating empty subsystems up front:

```text
archer/
  apps/web/                       V1
  services/core/                  V1
  connectors/filesystem/          V1
  connectors/web-search/          V1
  connectors/notes/               V1
  deployment/                     V1
  tests/                          V1, expands by phase
  docs/                           V1, expands by phase

  apps/desktop/                   V2
  apps/mobile/                    V2
  services/voice/                 V2
  services/manufacturing/         V2
  connectors/home-assistant/      V2
  connectors/archer-node/         V2
  connectors/printer/             V2 after validation
  hardware/                       V2 optional peripherals

  services/vision/                V3
  os/                             V3
```

Store model deployment manifests and revision pins in the repository; keep model weights, runtime databases, and credentials outside version control.

For a two-person team, V1 can divide Core/authentication/model/memory work from web UI/starter plugins/deployment, with one owner for shared contracts. V2 can evolve toward the original Brain/World split: one person handles reasoning, memory, permissions, and evaluation; the other handles devices, nodes, manufacturing, and client integration. Both own end-to-end acceptance checks.

Agree on interfaces before parallel implementation, avoid concurrent ownership of shared files, and review integration at each gate. This is a human team organization proposal, not an instruction to spawn agent workers automatically.

## Research candidates and release discipline

### Experimental runtime backends

The earlier document named DeepSeek Harness and Cordis. Treat those as unverified research candidates: confirm the official project, supported interfaces, license, maintenance, and compatibility before evaluating an adapter. V1 uses Archer's own bounded run loop; no speculative framework is a required dependency.

An optional backend must preserve Archer's permissions, account isolation, journal, cancellation, and recovery contracts. Compare candidates using the same task scenarios and report measured outcomes rather than illustrative scores presented as results.

### Release artifacts

At each phase's completion gate, freeze the agreed scope and produce installation/deployment instructions, example configuration without secrets, supported-platform notes, connector guidance, backup/restore instructions, measured acceptance results, known limitations, and a reproducible demonstration.

For V1, include the tested Spark model/container configuration, owner account setup, public hostname configuration, search credentials, optional cloud consent, and MCP review procedure. Do not describe the system as remotely usable until external-network verification succeeds.

### Boundaries that keep the roadmap achievable

- Build the secure assistant foundation before adding physical orchestration.
- Keep model providers replaceable and benchmark actual hardware.
- Add connectors when they enable useful, verified workflows.
- Keep learned preferences distinct from permissions and executable routines.
- Require verified integration paths for physical devices and printers.
- Reuse Core and account state across clients.
- Keep OS, webcam, LiDAR, and wearables in V3.
- Measure reliability and disclose manual steps; do not substitute simulated outcomes for hardware proof.

The original research's long-term thesis remains: Archer connects intent, reasoning, action, and memory. The three-phase roadmap makes that thesis deliverable, beginning with a secure local assistant that can be used from anywhere with an authenticated browser.
