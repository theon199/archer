"""Run with: python3 -m unittest discover -s scripts -p 'test_overnight.py'."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
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
        self.repo = Path(self.temporary.name)
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
                patch.object(overnight.subprocess, "run") as run:
            overnight.install(self.args)
        units = self.repo / ".config/systemd/user"
        service = (units / "archer-overnight.service").read_text()
        timer = (units / "archer-overnight.timer").read_text()
        self.assertIn("KillMode=control-group", service)
        self.assertIn("UnsetEnvironment=OPENAI_API_KEY CODEX_API_KEY OPENAI_BASE_URL", service)
        self.assertIn("OnUnitInactiveSec=60s", timer)
        self.assertEqual(run.call_count, 2)

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
