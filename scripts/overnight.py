#!/usr/bin/env python3
"""Account-quota-aware Codex exec checkpoints, optionally launched by a Linux timer.

No third-party Python packages. No credentials are read or copied. Quota reads use
the authenticated Codex app-server RPC; no model requests occur during a check.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"complete", "blocked", "attention", "expired", "stopped"}


class NotReady(RuntimeError):
    pass


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def terminate(process):
    """Interrupt the private child process group, then bound cleanup time."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            process.wait(timeout=5)
            return
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def quota_snapshot(codex):
    if any(os.environ.get(key) for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")):
        raise NotReady("Unset API-key/endpoint environment overrides; this runner uses ChatGPT account quotas.")
    process = subprocess.Popen(
        [codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = b""

    def send(value):
        process.stdin.write((json.dumps(value) + "\n").encode())
        process.stdin.flush()

    def receive(request_id):
        nonlocal pending
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("id") == request_id:
                    if "error" in message:
                        raise NotReady("Codex rejected the quota RPC; check CLI version and login.")
                    return message.get("result")
                if "id" in message and "method" in message:
                    # Quota reads must not elicit approvals, logins, or tool execution.
                    send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}})
            if not selector.select(timeout=min(1, max(0, deadline - time.monotonic()))):
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise NotReady("Codex app-server exited before returning quota information.")
            pending += chunk
            if len(pending) > 2_000_000:
                raise NotReady("Unexpectedly large quota RPC response.")
        raise NotReady("Quota RPC timed out; no coding run was authorized.")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "archer_overnight", "version": "1.0.0"}}})
        receive(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        snapshot = receive(2)
        if not isinstance(snapshot, dict):
            raise NotReady("Quota information is unavailable; sign in to Codex with ChatGPT.")
        return snapshot
    finally:
        selector.close()
        terminate(process)
        process.stdin.close()
        process.stdout.close()


def quota_decision(snapshot, reserve, now):
    """Conservatively gate every reported bucket, including the weekly window."""
    ordinary = snapshot.get("ordinaryUsageAllowed", "absent")
    if ordinary is None:
        raise NotReady("The service did not establish included-usage eligibility.")
    buckets = snapshot.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {"default": snapshot.get("rateLimits")}
    windows = []
    for bucket_name, bucket in buckets.items():
        if not isinstance(bucket, dict):
            raise NotReady("Malformed quota bucket; refusing to guess available usage.")
        if bucket.get("spendControlReached") is True:
            raise NotReady("An account spending control is reached; human review is required.")
        if bucket.get("rateLimitReachedType") not in (None, "rate_limit_reached"):
            raise NotReady("Account credit or spending restrictions require human review.")
        bucket_windows = []
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if window is None:
                continue
            if not isinstance(window, dict):
                raise NotReady("Malformed quota window.")
            used, reset = window.get("usedPercent"), window.get("resetsAt")
            if (type(used) not in (int, float) or not math.isfinite(used) or used < 0
                    or type(reset) not in (int, float) or not math.isfinite(reset) or reset <= 0):
                raise NotReady("Quota percentages or reset timestamps are missing/invalid.")
            item = {"bucket": bucket_name, "window": name,
                    "remaining_percent": max(0, 100 - used), "reset_at": reset,
                    "window_minutes": window.get("windowDurationMins")}
            windows.append(item)
            bucket_windows.append(item)
        if not bucket_windows:
            raise NotReady("A reported quota bucket has no usable window information.")
        if bucket.get("rateLimitReachedType") == "rate_limit_reached":
            if not any(item["remaining_percent"] <= reserve for item in bucket_windows):
                raise NotReady("Service reports a limit without a matching low window; human review is required.")
    low = [w for w in windows if w["remaining_percent"] <= reserve]
    if ordinary is False and not low:
        raise NotReady("The service denies included usage despite the reported percentages.")
    if low:
        # A past reset timestamp does NOT imply recovered quota. Re-read later.
        return max(now + 300, max(w["reset_at"] for w in low) + 120), windows
    return None, windows


def exec_command(codex, state, repo, report):
    base = [codex, "exec", "--approve-for-me", "-c", 'model_provider="openai"', "--disable", "goals", "--json",
            "--output-schema", str(repo / "scripts/overnight-result.schema.json"),
            "-o", str(report)]
    if state.get("thread_id"):
        return base + ["resume", state["thread_id"], "-"]
    return base + ["-"]


def run_checkpoint(args):
    repo, codex = args.repo.resolve(), args.codex
    folder = repo / ".archer-overnight"
    folder.mkdir(mode=0o700, exist_ok=True)
    with (folder / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another overnight worker owns the repository; skipping.")
            return
        state_path = folder / "state.json"
        if not state_path.exists():
            raise NotReady("No run configured. Run install first, or initialize with start.")
        state = json.loads(state_path.read_text())
        if (folder / "STOP").exists() or state.get("status") in TERMINAL:
            return
        now = time.time()
        if now >= state["until"]:
            state.update(status="expired", reason="Overnight execution window ended; objective may remain unfinished.")
            write_json(state_path, state)
            return
        if now < state.get("next_attempt", 0):
            return
        try:
            retry_at, windows = quota_decision(quota_snapshot(codex), args.reserve, now)
        except (NotReady, OSError) as error:
            state.update(status="attention", reason=str(error))
            write_json(state_path, state)
            print(state["reason"])
            return
        if retry_at:
            state.update(status="waiting", next_attempt=retry_at, windows=windows)
            write_json(state_path, state)
            print("Quota reserve reached; next eligibility check:", time.ctime(retry_at))
            return
        report = folder / "result.json"
        report.unlink(missing_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()}"
        transcript = folder / f"{stamp}.jsonl"
        state.update(status="running", next_attempt=0, windows=windows)
        write_json(state_path, state)
        prompt = (repo / "docs/dgx-goal.txt").read_text() + "\n\n" + (
            "This is one scheduled checkpoint of the full objective. Native Goal auto-continuation is disabled "
            "because the external runner owns scheduling. Inspect current files and docs/overnight-handoff.md, "
            "then complete one coherent implementation/verification increment, aiming for no more than 15 minutes. "
            "Update the handoff frequently so interruption loses minimal context. Never edit scripts/overnight.py, "
            "its tests, the result schema, timer units, or .archer-overnight/. Do not install another scheduler or "
            "spawn another agent. Return structured status continue, blocked, or complete with concrete evidence. "
            "Blocked means no independent work remains possible; complete requires every acceptance criterion."
        )
        interrupted = None
        with transcript.open("wb") as log, (folder / f"{stamp}.stderr.log").open("wb") as errors:
            child = subprocess.Popen(exec_command(codex, state, repo, report), cwd=repo,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                     start_new_session=True)
            selector = selectors.DefaultSelector()
            selector.register(child.stdout, selectors.EVENT_READ)
            child.stdin.write(prompt.encode())
            child.stdin.close()
            pending = b""
            started = time.monotonic()
            next_poll = started + args.poll_seconds

            def record(chunk):
                nonlocal pending
                log.write(chunk)
                log.flush()
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(event, dict) and event.get("type") == "thread.started" and event.get("thread_id"):
                        state["thread_id"] = event["thread_id"]
                        write_json(state_path, state)

            try:
                while child.poll() is None:
                    for key, _ in selector.select(timeout=1):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        record(chunk)
                    if (folder / "STOP").exists():
                        interrupted = "stopped"
                        break
                    if time.time() >= state["until"]:
                        interrupted = "expired"
                        break
                    if time.monotonic() - started >= args.chunk_seconds:
                        interrupted = "checkpoint_timeout"
                        break
                    if time.monotonic() >= next_poll:
                        try:
                            retry_at, windows = quota_decision(quota_snapshot(codex), args.reserve, time.time())
                            state["windows"] = windows
                        except (NotReady, OSError) as error:
                            interrupted = "quota_unavailable"
                            state["reason"] = str(error)
                            break
                        if retry_at:
                            state["next_attempt"] = retry_at
                            interrupted = "quota_low"
                            break
                        next_poll = time.monotonic() + args.poll_seconds
            finally:
                selector.close()
                terminate(child)
                # A tool process might still own stdout; do not block awaiting EOF.
                os.set_blocking(child.stdout.fileno(), False)
                remaining = child.stdout.read()
                if remaining:
                    record(remaining)
                child.stdout.close()
        state["last_transcript"] = str(transcript.relative_to(repo))
        if interrupted:
            state["status"] = {"stopped": "stopped", "expired": "expired", "quota_unavailable": "attention"}.get(
                interrupted, "waiting" if interrupted == "quota_low" else "ready")
            state["reason"] = state.get("reason") if interrupted == "quota_unavailable" else interrupted
            if interrupted == "checkpoint_timeout":
                state["failures"] = state.get("failures", 0) + 1
                state["next_attempt"] = time.time() + 600
                if state["failures"] >= 3:
                    state["status"] = "attention"
        else:
            try:
                outcome = json.loads(report.read_text())
                if (child.returncode or not isinstance(outcome, dict)
                        or outcome.get("status") not in {"continue", "complete", "blocked"}):
                    raise ValueError("Unsuccessful checkpoint")
                if outcome["status"] == "complete" and (
                    not outcome.get("evidence") or not (repo / "docs/overnight-handoff.md").exists()
                ):
                    raise ValueError("Completion lacks evidence/handoff")
                state.update(status="ready" if outcome["status"] == "continue" else outcome["status"],
                             result=outcome, failures=0)
            except (OSError, ValueError, TypeError):
                failures = state.get("failures", 0) + 1
                state.update(status="attention" if failures >= 3 else "waiting", failures=failures,
                             next_attempt=time.time() + 600, reason="Checkpoint failed; inspect private logs.")
        write_json(state_path, state)
        print("Checkpoint status:", state["status"])


def initialize(args):
    repo = args.repo.resolve()
    folder = repo / ".archer-overnight"
    folder.mkdir(mode=0o700, exist_ok=True)
    if (folder / "state.json").exists():
        raise NotReady("A run already exists. Stop the service and archive its state before starting another.")
    (folder / "STOP").unlink(missing_ok=True)
    write_json(folder / "state.json", {"status": "ready", "until": time.time() + args.hours * 3600,
                                       "next_attempt": 0, "failures": 0})


def unit_contents(args):
    """Render systemd directives using each directive's own parsing rules."""
    repo = args.repo.resolve()
    script, python = Path(__file__).resolve(), Path(sys.executable).resolve()

    def quote(value):
        value = str(value)
        if any(c in value for c in "\r\n\0"):
            raise NotReady("Systemd command arguments cannot contain newlines or NULs.")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    # WorkingDirectory does not perform ExecStart's shell-like unquoting.
    directory = str(repo)
    if directory != directory.strip() or any(c in directory for c in "\r\n\0\\"):
        raise NotReady("Checkout path cannot contain boundary whitespace, newlines, NULs, or backslashes.")
    command = " ".join(quote(x) for x in [python, script, "run", "--repo", repo, "--codex", args.codex,
                                         "--reserve", args.reserve])
    path_value = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    service = ("[Unit]\nDescription=Archer overnight coding checkpoint\n\n[Service]\nType=oneshot\n"
               f"WorkingDirectory={directory.replace('%', '%%')}\nExecStart={command}\n"
               f"Environment={quote('PATH=' + path_value)}\n"
               "UnsetEnvironment=OPENAI_API_KEY CODEX_API_KEY OPENAI_BASE_URL\nUMask=0077\nTimeoutStartSec=25min\n"
               "TimeoutStopSec=30s\nKillMode=control-group\n")
    timer = ("[Unit]\nDescription=Resume Archer coding checkpoints when eligible\n\n[Timer]\n"
             "OnActiveSec=10s\nOnUnitInactiveSec=60s\nAccuracySec=10s\n\n[Install]\nWantedBy=timers.target\n")
    return {"archer-overnight.service": service, "archer-overnight.timer": timer}


def install(args, repair=False):
    if sys.platform != "linux" or not shutil.which("systemctl"):
        raise NotReady("Install requires Linux with a working systemd user manager.")
    # Test account access before creating an unattended service. Low quota can wait.
    quota_decision(quota_snapshot(args.codex), args.reserve, time.time())
    if not shutil.which("systemd-analyze"):
        raise NotReady("systemd-analyze is required to validate the generated user units.")
    repo = args.repo.resolve()
    state_path = repo / ".archer-overnight/state.json"
    if state_path.exists() and not repair:
        raise NotReady("A run exists. Use repair to fix its units while preserving session state.")
    if repair and not state_path.exists():
        raise NotReady("No existing run to repair. Use install to create a new run.")
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    units = unit_contents(args)
    for name, content in units.items():
        target = unit_dir / name
        if target.is_symlink():
            raise NotReady(f"Refusing to replace symlink {target}.")
        if target.exists():
            existing = target.read_text()
            # Accept only our current unit or the original quoted-path variant.
            directory = str(repo).replace("%", "%%")
            legacy_directory = directory.replace('"', '\\"')
            legacy = content.replace(f"WorkingDirectory={directory}\n",
                                     f'WorkingDirectory="{legacy_directory}"\n')
            if not repair or existing not in (content, legacy):
                raise NotReady(f"Refusing to overwrite customized/unrecognized {name}; inspect it first.")

    # Validate in a temporary directory before replacing either installed file.
    with tempfile.TemporaryDirectory(prefix="archer-units-", dir=unit_dir) as staging:
        paths = []
        for name, content in units.items():
            path = Path(staging) / name
            path.write_text(content)
            paths.append(str(path))
        subprocess.run(["systemd-analyze", "--user", "verify", *paths], check=True)

    if repair:
        # A malformed unit may make stop return nonzero; the lock below still
        # prevents replacing units while a checkpoint owns this checkout.
        subprocess.run(["systemctl", "--user", "stop", "archer-overnight.timer"], check=False)
    folder = repo / ".archer-overnight"
    folder.mkdir(mode=0o700, exist_ok=True)
    with (folder / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NotReady("A checkpoint is running. Stop archer-overnight.service before repair.")
        for name, content in units.items():
            (unit_dir / name).write_text(content)
        if not repair:
            initialize(args)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        for name in units:
            loaded = subprocess.run(["systemctl", "--user", "show", name, "--property=LoadState", "--value"],
                                    check=True, capture_output=True, text=True)
            if loaded.stdout.strip() != "loaded":
                raise NotReady(f"{name} did not load: {loaded.stdout.strip()}. Inspect its user journal.")
        subprocess.run(["systemctl", "--user", "enable", "--now", "archer-overnight.timer"], check=True)
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "archer-overnight.timer"],
                                check=False)
        if active.returncode != 0:
            raise NotReady("Timer is not active. Inspect journalctl --user -u archer-overnight.timer.")
    print("Timer enabled and active. Verify the first checkpoint in the user journal before logging out.")
    if repair:
        state = json.loads(state_path.read_text())
        print(f"Preserved run status {state['status']} and deadline {time.ctime(state['until'])}.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "install", "repair", "start", "run", "status", "stop"])
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--reserve", type=float, default=15)
    parser.add_argument("--hours", type=float, default=12)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--chunk-seconds", type=int, default=1200)
    args = parser.parse_args()
    if not (0 < args.reserve < 100 and 0 < args.hours <= 72
            and 10 <= args.poll_seconds <= 300 and 30 <= args.chunk_seconds <= 1200):
        parser.error("Reserve must be 0–100, hours 0–72, poll 10–300s, and chunk 30–1200s.")
    args.repo = args.repo.resolve()
    try:
        if args.action == "check":
            retry, windows = quota_decision(quota_snapshot(args.codex), args.reserve, time.time())
            print(json.dumps({"eligible_now": retry is None, "next_check_at": retry, "windows": windows}, indent=2))
        elif args.action == "install":
            install(args)
        elif args.action == "repair":
            install(args, repair=True)
        elif args.action == "start":
            initialize(args)
        elif args.action == "run":
            run_checkpoint(args)
        elif args.action == "status":
            path = args.repo / ".archer-overnight/state.json"
            print(path.read_text() if path.exists() else "No overnight run configured.")
        else:
            folder = args.repo / ".archer-overnight"
            folder.mkdir(mode=0o700, exist_ok=True)
            (folder / "STOP").touch()
            print("Stop requested. Disable the timer/service as documented to stop immediately.")
    except (NotReady, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Not ready: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
