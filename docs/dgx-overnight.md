# Run Archer development overnight on DGX Spark

Checked October 23, 2026. This sets up a local coding workflow, not Archer's application scheduler.

## What is possible in the CLI

Interactive Codex supports `/goal` for a persistent objective. The CLI does not provide the app/web Scheduled management interface. A prompt alone cannot reliably wake a stopped CLI after a usage reset. [Goals](https://learn.chatgpt.com/docs/use-cases/follow-goals), [scheduled-task surface](https://learn.chatgpt.com/docs/automations)

This repository therefore supplies two alternatives:

- **Interactive goal:** start `codex --approve-for-me` in this directory, type `/goal `, and paste the content of `docs/dgx-goal.txt`. Keep the terminal session alive. This does not arm automatic post-reset resumption.
- **Unattended timer:** use the commands below. A Linux systemd user timer runs sequential Codex checkpoints against the same objective and resumes its saved session. Native Goal auto-continuation is disabled for these jobs, because the runner owns continuation and usage gating. Do not run an interactive goal against the same checkout at the same time.

## Prepare the DGX

```bash
git clone https://github.com/theon199/archer.git
cd archer
codex --version
codex login --device-auth
python3 scripts/overnight.py check
```

Use a current Linux ARM64 Codex installation supporting `--approve-for-me`, structured exec output, and app-server quota reads. The runner's CLI contract and a live read-only quota request were verified with Codex 0.163.0 on the development Mac. Check again on the DGX; its login, systemd configuration, packages, and GPU stack have not been verified here. This is personal local automation using your own login, not a public CI job. Never commit or copy authentication files into this repository.

`check` prints only usage windows, remaining percentages, reset timestamps, and eligibility. It makes no inference request. Sign in using your ChatGPT account. API-key-only use does not provide the intended account-window behavior, and this runner rejects API-key and custom-endpoint environment overrides. Account restrictions, unavailable usage information, or malformed responses stop execution for review.

Install Git, Python, uv, Node, and pnpm, and verify Docker/GPU permissions before leaving. Follow the existing lockfiles. A first supervised dependency/model smoke test is worthwhile; approvals or missing credentials may still block particular work. The architecture's local inference backend remains replaceable—do not disrupt an already working Ollama/SGLang endpoint merely to install a proposed vLLM recipe.

## Enable scheduled continuation

On the DGX, allow your user service manager to survive SSH logout:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

This is a one-time user-service setup action. Keep the DGX powered on and prevent suspend. Lingering keeps the user manager alive after logout; it does not wake a powered-off machine. [systemd loginctl](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)

Then, as your ordinary user, from this repository:

```bash
python3 scripts/overnight.py install --hours 12
systemctl --user status archer-overnight.timer
python3 scripts/overnight.py status
```

The installer writes only `archer-overnight.service` and `archer-overnight.timer` in your user systemd directory plus private local run state. It captures this checkout and the Python/Codex executable paths. Do not move the checkout while the timer is enabled. It refuses to overwrite existing units or an existing run. No timer is installed on the development Mac by these repository changes.

The units are checked with `systemd-analyze --user verify` before installation. The installer also checks both units' load state and confirms the timer is active before reporting success. An active timer does not by itself prove that a coding checkpoint succeeded; inspect the service journal and run status below.

### Repair an installation with a quoted WorkingDirectory

The original installer incorrectly wrote `WorkingDirectory="/home/sri/archer"`. Systemd treats those quote characters as part of this directive's path, causing a `bad-setting` service and an inactive timer. Update the checkout and repair the generated units:

```bash
git pull --ff-only
python3 scripts/overnight.py repair
systemctl --user status archer-overnight.timer archer-overnight.service --no-pager -l
python3 scripts/overnight.py status
journalctl --user -u archer-overnight.service -n 40 --no-pager
```

`repair` validates the replacement units, stops the timer, and refuses to replace units while a checkpoint holds the repository lock. It accepts only recognized generated units for this checkout and preserves the saved session, status, retry timestamp, and original deadline. It does not extend an expired run or restart a terminal status. If a checkpoint is running, stop `archer-overnight.service` first, then retry. Keep your existing `.archer-overnight/` directory. [WorkingDirectory syntax](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#WorkingDirectory=)

## Usage and recovery behavior

- Default reserve: **15% remaining**. Each checkpoint checks usage before starting and about once per minute while running. Interruption is best effort: a large in-flight request or use elsewhere on the same account can cross the reserve between checks. This is not a hard token/spending guarantee.
- The runner examines all reported quota buckets and both primary/secondary windows conservatively. A depleted weekly window can prevent continuation even after the five-hour window resets.
- Low capacity saves a retry timestamp using the latest relevant reported reset plus two minutes. The timer checks the saved timestamp about once per minute, then re-reads actual eligibility. It never assumes that a reset timestamp proves capacity has recovered.
- Missing quota information requires attention. The runner does not purchase credits, redeem reset credits, rotate accounts, or fall back to an API key or another provider.
- Checkpoints aim for one useful increment, with a 20-minute process limit. The goal and handoff keep the full V1 objective intact; a checkpoint is not a redefinition of completion. Interruptions may prevent a final handoff edit, so the next run also inspects the actual working tree.
- The session ID is persisted from Codex JSON events and explicitly resumed. The runner never uses `--last`, which could select an unrelated chat. Failed structured responses retry with a delay; three consecutive failed or timed-out checkpoints require attention.
- A file lock and a single systemd service prevent overlapping runner instances. This does not lock out a person or a separately launched coding agent; use a dedicated checkout.
- The default automation window is **12 hours**, configurable up to 72 hours with `--hours`. Expiry leaves an unfinished objective unfinished. Completion, a hard blocker, attention, or a stop request prevents further coding requests. The idle timer remains enabled until you disable it.

Codex documents explicit session resumption and structured output; app-server exposes account quota windows and reset times. The timer is a repository implementation around those interfaces, not a native CLI Scheduled feature. [Codex execution](https://learn.chatgpt.com/docs/non-interactive-mode), [quota API](https://learn.chatgpt.com/docs/app-server)

## Inspect, stop, and restart

```bash
python3 scripts/overnight.py status
journalctl --user -u archer-overnight.service -n 40 --no-pager
cat docs/overnight-handoff.md
```

Private transcripts, results, usage decisions, and state are stored in `.archer-overnight/`, which is ignored by Git and created with restrictive permissions. Logs can contain tool output; keep them private and review before sharing. The agent's completion report still needs the stated acceptance evidence; the runner itself does not certify application correctness.

To stop immediately:

```bash
python3 scripts/overnight.py stop
systemctl --user disable --now archer-overnight.timer
systemctl --user stop archer-overnight.service
```

To extend a finished/expired run, stop the service/timer first. Back up `.archer-overnight/` outside the checkout if you need the session/logs, remove the old local state and the two generated user unit files, then run `install --hours ...` again. The new session reads the existing code and handoff. Do not delete application data or authentication files.

No GitHub pushes, external publication, paid service provisioning, or real email/calendar actions are authorized by the overnight prompt. Optional missing integrations must be reported as unavailable while independent local work continues.

## Validation performed before pushing

The standalone unittest suite covers usage parsing, both quota windows, missing data, actual-reset waiting, subprocess RPC, session-ID resumption, mid-run usage interruption, loss of the usage monitor, completion evidence, deadline handling, locking, and generated timer units with a fake Codex executable. Repair tests cover the quoted-path regression, preserved state, refused custom units, failed validation, bad load states, and inactive timers. Systemd calls are mocked in the local suite; the installer performs real validation on Linux. A separate live read-only quota check passed against the installed CLI. No paid coding run or real Linux timer was started during this preparation; verify installation and the first checkpoint on the DGX before leaving it unattended.

```bash
python3 -m unittest discover -s scripts -p 'test_overnight.py'
```
