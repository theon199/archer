"""Run with: python3 -m unittest discover -s scripts -p 'test_overnight.py'."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("overnight", Path(__file__).with_name("overnight.py"))
overnight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overnight)


def snapshot(used=20, reset=5000, weekly_used=20):
    return {"ordinaryUsageAllowed": True, "rateLimits": {}, "rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": used, "resetsAt": reset, "windowDurationMins": 300},
        "secondary": {"usedPercent": weekly_used, "resetsAt": reset + 7000, "windowDurationMins": 10080}}}}


class QuotaTests(unittest.TestCase):
    def test_all_windows_healthy(self):
        retry, windows = overnight.quota_decision(snapshot(), 15, 1000)
        self.assertIsNone(retry)
        self.assertEqual([w["remaining_percent"] for w in windows], [80, 80])

    def test_short_window_waits_for_actual_reset_with_buffer(self):
        retry, _ = overnight.quota_decision(snapshot(85), 15, 1000)
        self.assertEqual(retry, 5120)

    def test_weekly_limit_prevents_five_hour_retry(self):
        retry, _ = overnight.quota_decision(snapshot(95, weekly_used=95), 15, 1000)
        self.assertEqual(retry, 12120)

    def test_past_reset_does_not_assume_recovery(self):
        retry, _ = overnight.quota_decision(snapshot(100, reset=100), 15, 1000)
        self.assertEqual(retry, 1300)  # Past timestamp requires a fresh check, never assumed recovery.

    def test_legacy_single_bucket(self):
        data = snapshot()
        data = {"rateLimits": data["rateLimitsByLimitId"]["codex"]}
        self.assertIsNone(overnight.quota_decision(data, 15, 1000)[0])

    def test_missing_percent_reset_and_unknown_eligibility_fail_closed(self):
        bad = [{}, {"rateLimits": {}}, {"rateLimits": {"primary": {"usedPercent": 0}}}]
        data = snapshot()
        data["ordinaryUsageAllowed"] = None
        bad.append(data)
        for item in bad:
            with self.subTest(item=item), self.assertRaises(overnight.NotReady):
                overnight.quota_decision(item, 15, 1000)

    def test_denied_usage_with_healthy_percentages_fails_closed(self):
        data = snapshot()
        data["ordinaryUsageAllowed"] = False
        with self.assertRaises(overnight.NotReady):
            overnight.quota_decision(data, 15, 1000)

    def test_unrelated_low_bucket_is_conservative(self):
        data = snapshot()
        data["rateLimitsByLimitId"]["other"] = {"primary": {"usedPercent": 99, "resetsAt": 9000}}
        self.assertEqual(overnight.quota_decision(data, 15, 1000)[0], 9120)

    def test_no_paid_credit_fallback(self):
        data = snapshot()
        data["rateLimitsByLimitId"]["codex"]["rateLimitReachedType"] = "workspace_owner_credits_depleted"
        with self.assertRaises(overnight.NotReady):
            overnight.quota_decision(data, 15, 1000)

    def test_nan_and_negative_percent_fail_closed(self):
        for used in (float("nan"), -1, True):
            with self.subTest(used=used), self.assertRaises(overnight.NotReady):
                overnight.quota_decision(snapshot(used), 15, 1000)


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
if sys.argv[1] == 'app-server':
    for line in sys.stdin:
        request = json.loads(line)
        if request.get('method') == 'initialize':
            print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
        elif request.get('method') == 'account/rateLimits/read':
            print(json.dumps({'id': request['id'], 'result': json.loads((root/'quota.json').read_text())}), flush=True)
else:
    sys.stdin.read()
    calls = root/'calls.json'
    old = json.loads(calls.read_text()) if calls.exists() else []
    calls.write_text(json.dumps(old + [sys.argv[1:]]))
    print(json.dumps({'type': 'thread.started', 'thread_id': 'test-session'}), flush=True)
    if (root/'slow').exists():
        time.sleep(5)
    outcome = json.loads((root/'outcome.json').read_text())
    pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text(json.dumps(outcome))
'''


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name).resolve()
        (self.repo / "docs").mkdir()
        (self.repo / "docs/dgx-goal.txt").write_text("Complete the test objective.")
        self.codex = self.repo / "fake-codex"
        self.codex.write_text(FAKE_CODEX)
        self.codex.chmod(0o700)
        (self.repo / "quota.json").write_text(json.dumps(snapshot(reset=time.time() + 5000)))
        (self.repo / "outcome.json").write_text(json.dumps({"status": "continue", "evidence": [], "summary": "checkpoint"}))
        self.args = argparse.Namespace(repo=self.repo, codex=str(self.codex), reserve=15, hours=12,
                                       poll_seconds=60, chunk_seconds=30)
        self.env = patch.dict(os.environ, {"CODEX_API_KEY": "", "OPENAI_API_KEY": "", "OPENAI_BASE_URL": ""})
        self.env.start()
        overnight.initialize(self.args)

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def state(self):
        return json.loads((self.repo / ".archer-overnight/state.json").read_text())

    def test_real_subprocess_rpc_and_session_resume(self):
        overnight.run_checkpoint(self.args)
        self.assertEqual(self.state()["thread_id"], "test-session")
        self.assertEqual(self.state()["status"], "ready")
        overnight.run_checkpoint(self.args)
        calls = json.loads((self.repo / "calls.json").read_text())
        self.assertNotIn("resume", calls[0])
        self.assertIn("resume", calls[1])
        self.assertIn("test-session", calls[1])
        self.assertIn("--approve-for-me", calls[0])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", calls[0])

    def test_low_quota_never_starts_coding(self):
        (self.repo / "quota.json").write_text(json.dumps(snapshot(95, reset=time.time() + 5000)))
        overnight.run_checkpoint(self.args)
        self.assertEqual(self.state()["status"], "waiting")
        self.assertFalse((self.repo / "calls.json").exists())
        # No RPC should be required before the saved retry timestamp.
        self.codex.unlink()
        overnight.run_checkpoint(self.args)

    def test_unknown_usage_stops_without_coding(self):
        (self.repo / "quota.json").write_text('{}')
        overnight.run_checkpoint(self.args)
        self.assertEqual(self.state()["status"], "attention")
        self.assertFalse((self.repo / "calls.json").exists())

    def test_low_quota_during_checkpoint_interrupts_and_waits(self):
        (self.repo / "slow").touch()
        self.args.poll_seconds = 0  # Drive the monitor immediately in this bounded fake-process test.
        data = snapshot(99, reset=time.time() + 5000)
        with patch.object(overnight, "quota_snapshot", side_effect=[snapshot(reset=time.time() + 5000), data]):
            overnight.run_checkpoint(self.args)
        self.assertEqual(self.state()["status"], "waiting")
        self.assertEqual(self.state()["reason"], "quota_low")
        self.assertGreater(self.state()["next_attempt"], time.time() + 4900)

    def test_lost_quota_monitor_stops_instead_of_spending_unobserved(self):
        (self.repo / "slow").touch()
        self.args.poll_seconds = 0
        with patch.object(overnight, "quota_snapshot", side_effect=[snapshot(), overnight.NotReady("offline")]):
            overnight.run_checkpoint(self.args)
        self.assertEqual(self.state()["status"], "attention")

    def test_timer_install_writes_scoped_units_without_starting_real_service(self):
        (self.repo / ".archer-overnight/state.json").unlink()
        with patch.object(overnight.sys, "platform", "linux"), \
                patch.object(overnight.shutil, "which", return_value="/usr/bin/systemctl"), \
                patch.object(overnight.Path, "home", return_value=self.repo), \
                patch.object(overnight, "quota_snapshot", return_value=snapshot()), \
                patch.object(overnight.subprocess, "run", side_effect=self.systemd_success) as run:
            overnight.install(self.args)
        units = self.repo / ".config/systemd/user"
        service = (units / "archer-overnight.service").read_text()
        timer = (units / "archer-overnight.timer").read_text()
        self.assertIn("KillMode=control-group", service)
        self.assertIn("UnsetEnvironment=OPENAI_API_KEY CODEX_API_KEY OPENAI_BASE_URL", service)
        self.assertIn("OnUnitInactiveSec=60s", timer)
        self.assertIn(f"WorkingDirectory={self.repo}\n", service)
        self.assertNotIn('WorkingDirectory="', service)
        self.assertEqual(run.call_count, 6)
        self.assertEqual(run.call_args_list[0].args[0][:3], ["systemd-analyze", "--user", "verify"])
        self.assertEqual(self.state()["status"], "ready")

    @staticmethod
    def systemd_success(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="loaded\n" if "show" in command else "")

    def install_context(self, repair=False, run=None):
        with patch.object(overnight.sys, "platform", "linux"), \
                patch.object(overnight.shutil, "which", return_value="/usr/bin/systemctl"), \
                patch.object(overnight.Path, "home", return_value=self.repo), \
                patch.object(overnight, "quota_snapshot", return_value=snapshot()), \
                patch.object(overnight.subprocess, "run", side_effect=run or self.systemd_success):
            overnight.install(self.args, repair=repair)

    def legacy_units(self):
        units = self.repo / ".config/systemd/user"
        units.mkdir(parents=True)
        for name, content in overnight.unit_contents(self.args).items():
            content = content.replace(f"WorkingDirectory={self.repo}\n", f'WorkingDirectory="{self.repo}"\n')
            (units / name).write_text(content)
        return units

    def test_repair_corrects_quoted_path_and_preserves_session_and_deadline(self):
        units = self.legacy_units()
        state = self.state()
        state.update(thread_id="existing-session", status="waiting", next_attempt=time.time() + 5000)
        overnight.write_json(self.repo / ".archer-overnight/state.json", state)
        self.install_context(repair=True)
        self.assertEqual(self.state(), state)
        self.assertIn(f"WorkingDirectory={self.repo}\n", (units / "archer-overnight.service").read_text())

    def test_repair_refuses_unrecognized_units(self):
        units = self.legacy_units()
        service = units / "archer-overnight.service"
        service.write_text("[Service]\nExecStart=/some/other/program\n")
        with self.assertRaisesRegex(overnight.NotReady, "unrecognized"):
            self.install_context(repair=True)
        self.assertIn("/some/other/program", service.read_text())

    def test_repair_preserves_installed_environment_and_executables_after_shell_changes(self):
        units = self.legacy_units()
        service = units / "archer-overnight.service"
        installed = service.read_text().replace(
            overnight.unit_word(overnight.Path(overnight.sys.executable).resolve()),
            '"/previous/python3"', 1).replace(
                overnight.unit_word(self.args.codex), '"/previous/codex"', 1)
        service.write_text(installed)
        before_state = self.state()
        with patch.dict(os.environ, {"PATH": "/different/shell/path"}):
            self.install_context(repair=True)
        expected = installed.replace(f'WorkingDirectory="{self.repo}"\n', f"WorkingDirectory={self.repo}\n")
        self.assertEqual(service.read_text(), expected)
        self.assertEqual(self.state(), before_state)
        # A second repair is harmless even with another shell environment.
        self.install_context(repair=True)
        self.assertEqual(service.read_text(), expected)

    def test_repair_refuses_service_for_another_checkout(self):
        units = self.legacy_units()
        service = units / "archer-overnight.service"
        service.write_text(service.read_text().replace(
            f'"--repo" "{self.repo}"', '"--repo" "/another/checkout"'))
        with self.assertRaisesRegex(overnight.NotReady, "unrecognized"):
            self.install_context(repair=True)

    def test_repair_refuses_duplicate_working_directory(self):
        units = self.legacy_units()
        service = units / "archer-overnight.service"
        service.write_text(service.read_text() + "WorkingDirectory=/somewhere/else\n")
        with self.assertRaisesRegex(overnight.NotReady, "unrecognized"):
            self.install_context(repair=True)

    def test_failed_validation_does_not_replace_units(self):
        units = self.legacy_units()
        before = (units / "archer-overnight.service").read_text()
        commands = []

        def invalid(command, **kwargs):
            commands.append(command)
            raise subprocess.CalledProcessError(1, command)

        with self.assertRaises(subprocess.CalledProcessError):
            self.install_context(repair=True, run=invalid)
        self.assertEqual(len(commands), 1)
        self.assertEqual((units / "archer-overnight.service").read_text(), before)

    def test_inactive_timer_does_not_report_success(self):
        self.legacy_units()

        def inactive(command, **kwargs):
            if "is-active" in command:
                return subprocess.CompletedProcess(command, 3)
            return self.systemd_success(command, **kwargs)

        with patch("builtins.print") as output:
            with self.assertRaisesRegex(overnight.NotReady, "not active"):
                self.install_context(repair=True, run=inactive)
        output.assert_not_called()

    def test_bad_load_state_prevents_activation(self):
        self.legacy_units()
        commands = []

        def bad_load(command, **kwargs):
            commands.append(command)
            if "show" in command:
                return subprocess.CompletedProcess(command, 0, stdout="bad-setting\n")
            return self.systemd_success(command, **kwargs)

        with self.assertRaisesRegex(overnight.NotReady, "did not load"):
            self.install_context(repair=True, run=bad_load)
        self.assertFalse(any("enable" in command for command in commands))

    def test_repair_refuses_running_checkpoint(self):
        self.legacy_units()
        with (self.repo / ".archer-overnight/lock").open("w") as lock:
            overnight.fcntl.flock(lock, overnight.fcntl.LOCK_EX | overnight.fcntl.LOCK_NB)
            with self.assertRaisesRegex(overnight.NotReady, "checkpoint is running"):
                self.install_context(repair=True)

    def test_directory_with_spaces_and_specifiers_is_unquoted(self):
        self.args.repo = self.repo / "space and 100%"
        service = overnight.unit_contents(self.args)["archer-overnight.service"]
        self.assertIn(f"WorkingDirectory={self.repo}/space and 100%%\n", service)
        self.assertIn(f'"{self.repo}/space and 100%%"', service)  # ExecStart still unquotes words.

    def test_completion_requires_evidence_and_handoff(self):
        (self.repo / "outcome.json").write_text(json.dumps({"status": "complete", "evidence": []}))
        overnight.run_checkpoint(self.args)
        self.assertEqual(self.state()["status"], "waiting")

    def test_stop_and_deadline_do_not_start_coding(self):
        state_path = self.repo / ".archer-overnight/state.json"
        state = self.state()
        state["until"] = time.time() - 1
        overnight.write_json(state_path, state)
        overnight.run_checkpoint(self.args)
        self.assertEqual(self.state()["status"], "expired")
        self.assertFalse((self.repo / "calls.json").exists())

    def test_lock_prevents_overlap(self):
        with (self.repo / ".archer-overnight/lock").open("w") as lock:
            overnight.fcntl.flock(lock, overnight.fcntl.LOCK_EX | overnight.fcntl.LOCK_NB)
            overnight.run_checkpoint(self.args)
        self.assertFalse((self.repo / "calls.json").exists())


if __name__ == "__main__":
    unittest.main()
