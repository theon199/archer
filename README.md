# Archer

A local-first personal assistant for DGX Spark. V1 covers private chat, approved tools, notes, preference memory, and an action journal. The application is in development; the architecture documents define the target rather than certify that every feature already works.

- [Architecture and tooling research](docs/system-architecture.md)
- [V1/V2/V3 roadmap](archer%20v1%20deepresearchplan.md)
- [DGX overnight setup and quota-aware continuation](docs/dgx-overnight.md)
- [Implementation assignment](docs/overnight-build-prompt.md)
- [Goal text](docs/dgx-goal.txt)

The existing backend uses uv and the frontend uses pnpm. See `Makefile` for development commands and `.env.example` for configuration. Review configuration and authentication before making services accessible outside the machine.

For scheduled overnight coding, read the DGX setup guide first. The included runner is a Linux systemd wrapper around Codex CLI, not the app's native Scheduled feature. It requires a current CLI, your own ChatGPT login, and readable account limits. It checks for a 15% usage reserve and resumes the saved session after eligibility returns; the default execution window is 12 hours.
