#!/usr/bin/env python3
"""Tests for wire.py (ladder-auto-wire).

Standard library only. Uses temporary files only and touches no
network. Each test runs wire.py as a subprocess, exactly as a user
would, so exit codes and stdout/stderr are covered as well as the
resulting ladder file.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WIRE = Path(__file__).resolve().parent.parent / "wire.py"
TODAY = "2026-11-25"


def make_resource(**overrides):
    """A valid fictional resource record; overrides replace fields."""
    record = {
        "name": "Example Free Models",
        "model": "example-model-free",
        "free_quota": {"amount": 50, "unit": "requests/day"},
        "expiry": "2027-06-30",
        "evidence": {
            "type": "live_test",
            "detail": "Called the list-models endpoint and got HTTP 200.",
            "at": "2026-11-20",
        },
    }
    record.update(overrides)
    return record


def make_provider(name, expiry, model="model-free", status="active"):
    return {
        "name": name,
        "model": model,
        "free_quota": {"amount": 10, "unit": "requests/day"},
        "expiry": expiry,
        "evidence": {
            "type": "live_test",
            "detail": "Called the list-models endpoint and got HTTP 200.",
            "at": "2026-11-20",
        },
        "status": status,
    }


class WireTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.resource_path = self.dir / "resource.json"
        self.ladder_path = self.dir / "ladder.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_json(self, path, data):
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def run_wire(self, resource=None, ladder=None, today=TODAY,
                 dry_run=False, resource_raw=None):
        """Write inputs, run wire.py, return (CompletedProcess, ladder)."""
        if resource_raw is not None:
            self.resource_path.write_text(resource_raw, encoding="utf-8")
        elif resource is not None:
            self.write_json(self.resource_path, resource)
        if ladder is not None:
            self.write_json(self.ladder_path, ladder)
        cmd = [sys.executable, str(WIRE),
               "--resource", str(self.resource_path),
               "--ladder", str(self.ladder_path)]
        if today is not None:
            cmd += ["--today", today]
        if dry_run:
            cmd.append("--dry-run")
        result = subprocess.run(cmd, capture_output=True, text=True)
        ladder_data = None
        if self.ladder_path.exists():
            ladder_data = json.loads(self.ladder_path.read_text(encoding="utf-8"))
        return result, ladder_data

    def providers(self, ladder_data):
        return ladder_data["providers"]

    def test_valid_resource_merges_and_sorts_by_expiry(self):
        ladder = {"providers": [
            make_provider("Sample Open Models", "2027-06-30"),
            make_provider("Demo Trial Models", "2027-03-01"),
        ]}
        resource = make_resource(expiry="2027-01-15")
        result, out = self.run_wire(resource, ladder)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [p["name"] for p in self.providers(out)]
        # Soonest-expiring active provider first.
        self.assertEqual(names, ["Example Free Models",
                                 "Demo Trial Models",
                                 "Sample Open Models"])
        self.assertTrue(all(p["status"] == "active"
                            for p in self.providers(out)))
        self.assertIn("Added", result.stdout)

    def test_missing_required_field_is_rejected(self):
        resource = make_resource()
        del resource["model"]
        result, out = self.run_wire(resource, {"providers": []})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.returncode, 2)
        self.assertIn("model", result.stderr)
        # The ladder must be left untouched on validation failure.
        self.assertEqual(self.providers(out), [])

    def test_invalid_expiry_is_rejected(self):
        resource = make_resource(expiry="2027-02-30")  # No such date.
        result, _ = self.run_wire(resource, {"providers": []})
        self.assertEqual(result.returncode, 2)
        self.assertIn("expiry", result.stderr)

        resource = make_resource(expiry="30/06/2027")  # Wrong format.
        result, _ = self.run_wire(resource, {"providers": []})
        self.assertEqual(result.returncode, 2)
        self.assertIn("expiry", result.stderr)

    def test_expired_resource_is_added_as_degraded(self):
        ladder = {"providers": [make_provider("Sample Open Models", "2027-06-30")]}
        resource = make_resource(expiry="2026-11-01")  # Before --today.
        result, out = self.run_wire(resource, ladder)
        self.assertEqual(result.returncode, 0, result.stderr)
        providers = self.providers(out)
        new = next(p for p in providers if p["name"] == "Example Free Models")
        self.assertEqual(new["status"], "degraded")
        # Degraded entries sort after active ones, whatever the expiry.
        self.assertEqual([p["name"] for p in providers],
                         ["Sample Open Models", "Example Free Models"])
        self.assertIn("Degraded", result.stdout)

    def test_existing_name_updates_instead_of_duplicating(self):
        ladder = {"providers": [make_provider("Example Free Models", "2027-01-15")]}
        resource = make_resource(model="example-model-free-v2",
                                 expiry="2027-12-31",
                                 free_quota={"amount": 99, "unit": "requests/day"})
        result, out = self.run_wire(resource, ladder)
        self.assertEqual(result.returncode, 0, result.stderr)
        providers = self.providers(out)
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["model"], "example-model-free-v2")
        self.assertEqual(providers[0]["expiry"], "2027-12-31")
        self.assertEqual(providers[0]["free_quota"]["amount"], 99)
        self.assertIn("Updated", result.stdout)
        self.assertNotIn("Added", result.stdout)

    def test_dry_run_writes_nothing(self):
        ladder = {"providers": [make_provider("Sample Open Models", "2027-06-30")]}
        self.write_json(self.ladder_path, ladder)
        before = self.ladder_path.read_text(encoding="utf-8")
        resource = make_resource(expiry="2027-01-15")
        result, _ = self.run_wire(resource, ladder=None, dry_run=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ladder_path.read_text(encoding="utf-8"), before)
        self.assertIn("DRY RUN", result.stdout)
        self.assertIn("Example Free Models", result.stdout)

    def test_today_is_honored(self):
        # Expiring "tomorrow" relative to --today stays active.
        resource = make_resource(expiry="2026-11-26")
        result, out = self.run_wire(resource, {"providers": []},
                                    today="2026-11-25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.providers(out)[0]["status"], "active")

        # The same expiry is "yesterday" relative to a later --today.
        result, out = self.run_wire(resource, {"providers": []},
                                    today="2026-11-27")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.providers(out)[0]["status"], "degraded")

    def test_unknown_evidence_type_is_rejected(self):
        resource = make_resource(evidence={
            "type": "rumor",
            "detail": "Someone said it is free.",
            "at": "2026-11-20",
        })
        result, _ = self.run_wire(resource, {"providers": []})
        self.assertEqual(result.returncode, 2)
        self.assertIn("evidence.type", result.stderr)

    def test_missing_free_quota_is_rejected(self):
        resource = make_resource()
        del resource["free_quota"]
        result, _ = self.run_wire(resource, {"providers": []})
        self.assertEqual(result.returncode, 2)
        self.assertIn("free_quota", result.stderr)

    def test_free_quota_string_is_normalized(self):
        resource = make_resource(free_quota="50 requests/day")
        result, out = self.run_wire(resource, {"providers": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.providers(out)[0]["free_quota"],
                         {"amount": 50, "unit": "requests/day"})

    def test_missing_ladder_file_starts_empty(self):
        # Do not create the ladder file at all before running.
        self.assertFalse(self.ladder_path.exists())
        resource = make_resource()
        result, out = self.run_wire(resource, ladder=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("did not exist", result.stdout)
        self.assertEqual(len(self.providers(out)), 1)
        self.assertEqual(self.providers(out)[0]["name"], "Example Free Models")


if __name__ == "__main__":
    unittest.main()
