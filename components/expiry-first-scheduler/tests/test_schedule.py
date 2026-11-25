#!/usr/bin/env python3
"""Tests for the expiry-first free-quota scheduler.

unittest, standard library only, temp files only, zero network.
"Today" is injected as a fixed date in every test so results are
deterministic whatever day the suite runs on.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schedule import (  # noqa: E402
    Scheduler,
    SchedulerError,
    load_providers,
    load_state_file,
    schedule,
)

TODAY = date(2026, 11, 25)
TODAY_STR = "2026-11-25"


def make_providers():
    """Fictional providers with distinct expiries and budgets."""
    return load_providers([
        {
            "name": "alpha",
            "model": "example-model-a",
            "endpoint": "https://api.example.invalid/v1",
            "expiry": "2026-12-05",
            "daily_free_budget": {"amount": 2, "unit": "requests/day"},
        },
        {
            "name": "beta",
            "model": "example-model-b",
            "endpoint": "https://api.example.invalid/v1",
            "expiry": "2027-01-15",
            "daily_free_budget": {"amount": 50, "unit": "requests/day"},
        },
        {
            "name": "gamma",
            "model": "example-model-c",
            "endpoint": "https://api.example.invalid/v1",
            "expiry": "2027-03-01",
            "daily_free_budget": {"amount": 50, "unit": "requests/day"},
        },
        {
            "name": "old",
            "model": "example-model-a",
            "endpoint": "https://api.example.invalid/v1",
            "expiry": "2026-11-01",
            "daily_free_budget": {"amount": 50, "unit": "requests/day"},
        },
    ])


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "scheduler-state.json"
        self.providers = make_providers()

    def new_scheduler(self, today=TODAY):
        return Scheduler(self.providers, state_path=self.state_path, today=today)


class TestOrderingAndExclusion(SchedulerTestCase):
    def test_expiry_sorting_soonest_first(self):
        result = schedule(self.providers, {}, today=TODAY)
        self.assertEqual(result["usable"], ["alpha", "beta", "gamma"])

    def test_expiry_tie_broken_by_name(self):
        providers = load_providers([
            {"name": "zeta", "expiry": "2027-01-01",
             "daily_free_budget": {"amount": 10, "unit": "requests/day"}},
            {"name": "apple", "expiry": "2027-01-01",
             "daily_free_budget": {"amount": 10, "unit": "requests/day"}},
        ])
        self.assertEqual(schedule(providers, {}, today=TODAY)["usable"],
                         ["apple", "zeta"])

    def test_expiring_today_is_still_usable(self):
        providers = load_providers([
            {"name": "today-only", "expiry": TODAY_STR,
             "daily_free_budget": {"amount": 10, "unit": "requests/day"}},
        ])
        self.assertEqual(schedule(providers, {}, today=TODAY)["usable"],
                         ["today-only"])

    def test_expired_provider_excluded_with_reason(self):
        result = schedule(self.providers, {}, today=TODAY)
        self.assertNotIn("old", result["usable"])
        self.assertIn({"name": "old", "reason": "expired"}, result["degraded"])

    def test_over_budget_excluded_after_record_use_reaches_budget(self):
        sched = self.new_scheduler()
        self.assertIn("alpha", sched.schedule()["usable"])
        sched.record_use("alpha")  # 1 of 2
        self.assertIn("alpha", sched.schedule()["usable"])
        sched.record_use("alpha")  # 2 of 2 -- budget reached
        result = sched.schedule()
        self.assertNotIn("alpha", result["usable"])
        self.assertIn({"name": "alpha", "reason": "over_budget"}, result["degraded"])

    def test_record_use_with_n(self):
        sched = self.new_scheduler()
        entry = sched.record_use("beta", n=5)
        self.assertEqual(entry["today_used"], 5)
        entry = sched.record_use("beta", n=3)
        self.assertEqual(entry["today_used"], 8)

    def test_dead_provider_excluded_until_report_alive(self):
        sched = self.new_scheduler()
        sched.report("beta", "dead")
        result = sched.schedule()
        self.assertNotIn("beta", result["usable"])
        self.assertIn({"name": "beta", "reason": "dead"}, result["degraded"])
        sched.report("beta", "alive")
        self.assertIn("beta", sched.schedule()["usable"])

    def test_report_quota_exceeded_exhausts_for_the_day(self):
        sched = self.new_scheduler()
        entry = sched.report("beta", "quota_exceeded")
        self.assertEqual(entry["today_used"], 50)  # == beta's budget amount
        result = sched.schedule()
        self.assertNotIn("beta", result["usable"])
        self.assertIn({"name": "beta", "reason": "over_budget"}, result["degraded"])

    def test_report_used_is_alias_for_record_use(self):
        sched = self.new_scheduler()
        entry = sched.report("beta", "used")
        self.assertEqual(entry["today_used"], 1)
        sched.report("beta", "used")
        self.assertEqual(sched.state["beta"]["today_used"], 2)

    def test_report_dead_sets_dead_since(self):
        sched = self.new_scheduler()
        entry = sched.report("beta", "dead")
        self.assertTrue(entry["dead"])
        self.assertEqual(entry["dead_since"], TODAY_STR)

    def test_unknown_provider_and_outcome_are_errors(self):
        sched = self.new_scheduler()
        with self.assertRaises(SchedulerError):
            sched.record_use("no-such-provider")
        with self.assertRaises(SchedulerError):
            sched.report("beta", "exploded")
        with self.assertRaises(SchedulerError):
            sched.record_use("beta", n=0)


class TestStatePersistence(SchedulerTestCase):
    def test_shared_state_persists_across_instances(self):
        first = self.new_scheduler()
        first.record_use("beta", n=7)
        first.report("gamma", "dead")
        # A brand-new Scheduler on the same state file sees the same usage.
        second = self.new_scheduler()
        self.assertEqual(second.state["beta"]["today_used"], 7)
        self.assertTrue(second.state["gamma"]["dead"])
        result = second.schedule()
        self.assertNotIn("gamma", result["usable"])
        self.assertIn({"name": "gamma", "reason": "dead"}, result["degraded"])

    def test_state_file_is_valid_json_with_expected_shape(self):
        sched = self.new_scheduler()
        sched.record_use("beta", n=2)
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("beta", raw)
        self.assertEqual(raw["beta"]["today_used"], 2)
        self.assertEqual(raw["beta"]["last_used_date"], TODAY_STR)
        self.assertIs(raw["beta"]["dead"], False)
        self.assertIsNone(raw["beta"]["dead_since"])

    def test_date_rollover_resets_today_used(self):
        sched = self.new_scheduler(today=TODAY)
        sched.record_use("beta", n=50)  # exhausts beta today
        self.assertIn({"name": "beta", "reason": "over_budget"},
                      sched.schedule()["degraded"])
        # Next day, same state file: the count resets, beta is usable again.
        next_day = self.new_scheduler(today=date(2026, 11, 26))
        self.assertIn("beta", next_day.schedule()["usable"])
        entry = next_day.record_use("beta", n=1)
        self.assertEqual(entry["today_used"], 1)  # reset to n, not 51
        self.assertEqual(entry["last_used_date"], "2026-11-26")

    def test_stale_state_counts_as_zero_without_recording(self):
        # State written yesterday with a full count must not block today.
        state = {"beta": {"today_used": 50, "last_used_date": "2026-11-24",
                          "dead": False, "dead_since": None}}
        result = schedule(self.providers, state, today=TODAY)
        self.assertIn("beta", result["usable"])

    def test_missing_state_file_starts_fresh(self):
        sched = self.new_scheduler()  # state file does not exist yet
        self.assertEqual(sched.schedule()["usable"], ["alpha", "beta", "gamma"])
        sched.record_use("beta", n=1)  # first write creates the file
        self.assertTrue(self.state_path.exists())

    def test_corrupt_state_file_starts_fresh_with_warning(self):
        self.state_path.write_text("{not valid json!!!", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            state, warning = load_state_file(self.state_path, today=TODAY)
        self.assertEqual(state, {})
        self.assertIsNotNone(warning)
        self.assertIn("starting fresh", warning)
        # A Scheduler on the corrupt file also starts fresh, never crashes.
        stderr2 = io.StringIO()
        with redirect_stderr(stderr2):
            sched = self.new_scheduler()
        self.assertEqual(sched.schedule()["usable"], ["alpha", "beta", "gamma"])
        self.assertIn("warning", stderr2.getvalue())

    def test_wrong_shape_state_file_starts_fresh(self):
        self.state_path.write_text("[1, 2, 3]", encoding="utf-8")
        state, warning = load_state_file(self.state_path, today=TODAY)
        self.assertEqual(state, {})
        self.assertIsNotNone(warning)


class TestConfigValidation(unittest.TestCase):
    def test_duplicate_names_rejected(self):
        with self.assertRaises(SchedulerError):
            load_providers([
                {"name": "dup", "expiry": "2027-01-01",
                 "daily_free_budget": {"amount": 1, "unit": "requests/day"}},
                {"name": "dup", "expiry": "2027-02-01",
                 "daily_free_budget": {"amount": 1, "unit": "requests/day"}},
            ])

    def test_bad_expiry_and_budget_rejected(self):
        with self.assertRaises(SchedulerError):
            load_providers([{"name": "x", "expiry": "not-a-date",
                             "daily_free_budget": {"amount": 1, "unit": "requests/day"}}])
        with self.assertRaises(SchedulerError):
            load_providers([{"name": "x", "expiry": "2027-01-01",
                             "daily_free_budget": {"amount": -1, "unit": "requests/day"}}])
        with self.assertRaises(SchedulerError):
            load_providers([{"name": "x", "expiry": "2027-01-01"}])  # missing budget


class TestCli(SchedulerTestCase):
    def _run_cli(self, *args):
        script = Path(__file__).resolve().parent.parent / "schedule.py"
        return subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True, text=True, cwd=self.tmp.name,
        )

    def _write_providers_file(self):
        path = Path(self.tmp.name) / "providers.json"
        path.write_text(json.dumps({
            "providers": [
                {"name": "alpha", "model": "example-model-a",
                 "endpoint": "https://api.example.invalid/v1",
                 "expiry": "2026-12-05",
                 "daily_free_budget": {"amount": 2, "unit": "requests/day"}},
                {"name": "beta", "model": "example-model-b",
                 "endpoint": "https://api.example.invalid/v1",
                 "expiry": "2027-01-15",
                 "daily_free_budget": {"amount": 50, "unit": "requests/day"}},
                {"name": "old", "model": "example-model-a",
                 "endpoint": "https://api.example.invalid/v1",
                 "expiry": "2026-11-01",
                 "daily_free_budget": {"amount": 50, "unit": "requests/day"}},
            ]
        }), encoding="utf-8")
        return path

    def test_cli_order_use_report_round_trip(self):
        providers = self._write_providers_file()
        state = str(self.state_path)
        base = ["--providers", str(providers), "--state", state,
                "--today", TODAY_STR]

        run = self._run_cli(*base, "order")
        self.assertEqual(run.returncode, 0, run.stderr)
        first = json.loads(run.stdout)
        self.assertEqual(first["usable"], ["alpha", "beta"])
        self.assertIn({"name": "old", "reason": "expired"}, first["degraded"])

        # Exhaust alpha's budget of 2 via the CLI.
        for _ in range(2):
            run = self._run_cli(*base, "use", "--provider", "alpha")
            self.assertEqual(run.returncode, 0, run.stderr)
        run = self._run_cli(*base, "order")
        second = json.loads(run.stdout)
        self.assertEqual(second["usable"], ["beta"])
        self.assertIn({"name": "alpha", "reason": "over_budget"}, second["degraded"])

        # Mark beta dead via the CLI, then bring it back.
        run = self._run_cli(*base, "report", "--provider", "beta", "--outcome", "dead")
        self.assertEqual(run.returncode, 0, run.stderr)
        run = self._run_cli(*base, "order")
        self.assertNotIn("beta", json.loads(run.stdout)["usable"])
        run = self._run_cli(*base, "report", "--provider", "beta", "--outcome", "alive")
        self.assertEqual(run.returncode, 0, run.stderr)
        run = self._run_cli(*base, "order")
        self.assertIn("beta", json.loads(run.stdout)["usable"])

    def test_cli_unknown_provider_exits_2(self):
        providers = self._write_providers_file()
        run = self._run_cli("--providers", str(providers), "--state", str(self.state_path),
                            "use", "--provider", "ghost")
        self.assertEqual(run.returncode, 2)
        self.assertIn("unknown provider", run.stderr)

    def test_cli_missing_providers_file_exits_2(self):
        run = self._run_cli("--providers", str(Path(self.tmp.name) / "nope.json"),
                            "--state", str(self.state_path), "order")
        self.assertEqual(run.returncode, 2)
        self.assertIn("not found", run.stderr)

    def test_cli_demo_runs_end_to_end(self):
        run = self._run_cli("demo")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("over_budget", run.stdout)
        self.assertIn("dead", run.stdout)
        self.assertIn("example-free-early", run.stdout)
        self.assertIn("Demo done.", run.stdout)


class TestDemo(unittest.TestCase):
    def test_demo_runs_end_to_end(self):
        from schedule import run_demo
        stdout = io.StringIO()
        with redirect_stderr(io.StringIO()):
            with mock.patch("sys.stdout", stdout):
                self.assertEqual(run_demo(), 0)
        out = stdout.getvalue()
        self.assertIn("example-free-early", out)
        self.assertIn("over_budget", out)
        self.assertIn("dead", out)
        self.assertIn("expired", out)
        self.assertIn("Demo done.", out)


if __name__ == "__main__":
    unittest.main()
