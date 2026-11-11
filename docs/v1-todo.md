# V1 MVP checklist

V1 is a single-owner assistant accessed privately over Tailscale. The existing native service is an acceptable deployment. The goal is to verify useful everyday workflows and fix problems that prevent their use, then move on to features. Physical hardware-failure tests, exhaustive model provenance, and a Compose migration are not V1 gates.

- [x] **Cited web research:** A real browser run used the local model and Tavily and cited returned sources. The answer simplified a technical tradeoff; one example is not a general accuracy guarantee. See [browser verification](browser-verification.md).
- [x] **Core workflows:** On 2026-11-08 an isolated browser and real local inference read an approved file, created a note, displayed the journal, changed a memory setting, and revoked a tool permission. A separate browser run checked learning, correction, deletion and use of saved preferences. The backend suite passed 201 tests and frontend suite passed 3. Verification receipts are retained locally.
- [x] **Private access and backup:** On 2026-11-08 the live service reported ready; both loopback and the tailnet-only route denied unauthenticated conversation access. The daily local backup timer was active and its latest backup reported success. Backups remain on this machine with seven-day retention.
- [x] **External demonstration:** On 2026-11-08 the owner reported successful external access through Tailscale, useful web research with citations, and reopening the saved conversation. The owner also confirmed the updated response formatting looked good.

Detailed verification receipts are retained locally as historical context rather than additional MVP requirements. Voice, devices, native apps, public registration and multi-user onboarding are future scope.
