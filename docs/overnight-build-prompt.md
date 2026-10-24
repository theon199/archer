# Overnight Archer V1 implementation prompt

Paste the content below into a coding-agent session running in the Archer repository on DGX Spark. This is an implementation assignment, not another research-only request. Keep the architecture and roadmap files beside it. Hardware and login readiness must be checked before leaving the run unattended.

---

Implement Archer V1 in this repository using `docs/system-architecture.md` and `archer v1 deepresearchplan.md`. Follow applicable AGENTS.md instructions. Use the architecture document's confirmed user requirements where the older roadmap is less specific: one person, general life management, local inference by default, and under $10/month as the external-service target. Do not provision paid services or enable cloud inference during this run.

Work through implementation and verification autonomously for this assignment. A single agent is sufficient; do not spawn subagents. Make routine implementation decisions and record them. Do not stop for optional product preferences. Missing credentials or permissions are blockers for the affected integration, not a reason to abandon independent application work. Never bypass a denied permission or silently replace a real integration with a successful fake result.

## Establish the environment

1. Inspect repository state and instructions. Preserve existing user changes. Verify the architecture and roadmap files are present. Do not use the macOS paths from an earlier conversation; discover the actual project location on this machine.
2. Record OS, CPU architecture, available system memory, GPU/driver visibility, free disk, Docker access, and running inference workloads. Do not stop existing services, change drivers, reboot, modify host networking, or make privileged host changes.
3. Check availability of Git, a compatible Python, uv, Node, pnpm, and Docker Compose. Inspect existing lockfiles before dependency commands. Use pnpm exclusively for a pnpm-managed project and uv for the Python project. If no lockfiles exist, establish these choices and commit their lockfiles with the implementation.
4. Revalidate proposed versions and model recipes using current official documentation. Treat version numbers and model names in the design as candidates, not proof of compatibility or availability. Pin exact compatible versions and document substitutions.

## Deliver in working increments

1. Inspect and continue the existing React/TypeScript browser application and FastAPI backend. Establish a tested baseline, preserve useful existing work, and add missing migrations, configuration, and development commands. Do not recreate the project from scratch.
2. Implement saved conversations, a durable database-backed run queue, streamed events, reconnection, cancellation, bounded execution, and a provider adapter. Establish a real local inference connection when possible. Keep a deterministic fake provider exclusively for explicit development/test configuration; production must fail clearly when inference is unavailable.
3. Implement owner setup, secure sessions, account ownership checks, and isolation tests before enabling external access.
4. Add approved read-only files, personal notes with revisions and idempotent changes, a permission-checked tool registry, and an accurate action journal. Add search and reviewed MCP configuration through the same authorization boundary. Missing search credentials must produce an unavailable status and must not prevent file/notes work.
5. Add preference extraction with evidence, correction, deletion suppression, disable-learning controls, and lower priority than interactive work. Suggested routines cannot schedule or execute themselves.
6. Add single-host deployment configuration, private service binding, health checks, persistent volumes, and backup/restore instructions. Leave public hosting disabled until its credentials, authentication, and external-network verification are ready.

Focus on a complete local vertical slice before spreading work across features. Preserve the original V1 scope; voice, unrestricted shell/code execution, workstation/home control, manufacturing, native apps, an OS, and spatial interfaces are later work.

## Local model work

If a suitable local endpoint already exists, test it without disrupting other workloads. Otherwise, evaluate one compatible model recipe first, only after checking storage and memory. Model downloads are allowed within available capacity; never delete user data to make room. If installation requires a privileged change or an interactive login, record the exact blocker and continue independent application work.

Keep executable model code, templates, parsers, and images pinned and reviewed. Do not expose a model port publicly or mount the host filesystem broadly. Demonstrate ordinary inference, structured tool calling, cancellation, and failure reporting before attempting broader benchmarks. Limit repeated failures and avoid an unbounded overnight model-download or tuning loop.

If local inference cannot be established, finish and verify the application against the explicitly labeled test adapter, then report local inference as blocked. Do not describe test-adapter results as successful Spark inference.

## Verification and handoff

Run meaningful backend and browser checks for streaming, reconnect, session/ownership enforcement, tool grants, note mutation, duplicate submissions, cancellation, unknown write outcomes, and memory corrections/deletions. Run real-model checks where available. Fix failures and retest the affected behavior. Never fabricate screenshots, benchmark values, tool outcomes, or passing tests.

Keep recoverable progress in the working tree and write `docs/overnight-handoff.md` as work proceeds. Include start/finish times, what works, commands to run it, actual tests and results, exact model/container revisions, unresolved blockers, and the next concrete step. Keep credentials and personal content out of logs. Do not push, publish, purchase, send messages, or change external accounts.

Complete the bounded assignment or stop with an accurate handoff when a hard blocker prevents further independent work. An unattended agent may finish before morning; it must not invent extra work or retry indefinitely merely to consume the night. Full production readiness and completion in one night are not guaranteed.
