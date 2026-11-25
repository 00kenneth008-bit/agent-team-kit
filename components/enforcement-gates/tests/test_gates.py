"""Tests for gates.py (stdlib unittest only).

No test calls any real external API. The second-model hook is tested only
in its skipped state and against an unreachable localhost address, so no
network service is ever contacted.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATES_PY = ROOT / "gates.py"
EXAMPLES = ROOT / "examples"

sys.path.insert(0, str(ROOT))
import gates as gates_mod  # noqa: E402


def clean_env(extra=None):
    """A copy of os.environ with all REVIEW_* hook vars removed."""
    env = dict(os.environ)
    for key in ("REVIEW_API_URL", "REVIEW_API_KEY", "REVIEW_MODEL"):
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


class CliMixin:
    def run_cli(self, *args, fmt="json", env_extra=None):
        proc = subprocess.run(
            [sys.executable, str(GATES_PY), *args, "--format", fmt],
            capture_output=True, text=True, timeout=60,
            env=clean_env(env_extra),
        )
        return proc

    def run_gates(self, standard, deliverable, extra=None, fmt="json",
                  env_extra=None):
        args = ["--standard", str(standard),
                "--deliverable", str(deliverable)]
        if extra:
            args += extra
        return self.run_cli(*args, fmt=fmt, env_extra=env_extra)


class TestGate1(CliMixin, unittest.TestCase):
    def test_veto_failure_blocks_at_gate1(self):
        proc = self.run_gates(EXAMPLES / "standard-text.example.json",
                              EXAMPLES / "does-not-exist.txt")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertEqual(report["final"], "blocked")
        self.assertEqual(len(report["gates"]), 1,
                         "a blocked gate 1 must stop the flow right there")
        gate1 = report["gates"][0]
        self.assertEqual(gate1["stage"], "self")
        self.assertEqual(gate1["verdict"], "blocked")
        self.assertIn("veto", gate1["reason"])
        veto = {v["id"]: v for v in report["veto"]}
        self.assertFalse(veto["file-exists"]["passed"])

    def test_score_below_80_blocks_at_gate1(self):
        proc = self.run_gates(EXAMPLES / "standard-text.example.json",
                              EXAMPLES / "sample-article-short.txt")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertEqual(len(report["gates"]), 1)
        gate1 = report["gates"][0]
        self.assertEqual(gate1["verdict"], "blocked")
        self.assertIn("below pass mark", gate1["reason"])
        self.assertLess(report["score"], report["pass_score"])
        self.assertTrue(report["deductions"])


class TestGate2(CliMixin, unittest.TestCase):
    def _write_score(self, tmp, name, score):
        path = Path(tmp) / name
        path.write_text(json.dumps({"score": score}), encoding="utf-8")
        return path

    def test_score_gap_over_10_sends_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager_score = self._write_score(tmp, "manager.json", 85)
            proc = self.run_gates(
                EXAMPLES / "standard-text.example.json",
                EXAMPLES / "sample-article.txt",
                ["--manager-score", str(manager_score)])
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["verdict"], "SEND_BACK")
            self.assertEqual(report["final"], "blocked")
            self.assertEqual(len(report["gates"]), 2,
                             "send-back at gate 2 must not run gate 3")
            gate2 = report["gates"][1]
            self.assertEqual(gate2["stage"], "manager")
            self.assertEqual(gate2["verdict"], "send_back")
            self.assertIn("score gap 15.0 > 10", gate2["reason"])

    def test_gap_within_10_proceeds_to_gate3(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager_score = self._write_score(tmp, "manager.json", 92)
            proc = self.run_gates(
                EXAMPLES / "standard-text.example.json",
                EXAMPLES / "sample-article.txt",
                ["--manager-score", str(manager_score)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["verdict"], "PASS")
            self.assertEqual(len(report["gates"]), 3)
            gate2 = report["gates"][1]
            self.assertEqual(gate2["verdict"], "pass")
            self.assertIn("score gap 8.0 within 10", gate2["reason"])
            self.assertEqual(report["gates"][2]["stage"], "final")
            self.assertEqual(report["gates"][2]["verdict"], "pass")

    def test_self_score_file_supplies_s1(self):
        with tempfile.TemporaryDirectory() as tmp:
            self_score = self._write_score(tmp, "self.json", 90)
            manager_score = self._write_score(tmp, "manager.json", 100)
            proc = self.run_gates(
                EXAMPLES / "standard-text.example.json",
                EXAMPLES / "sample-article.txt",
                ["--self-score", str(self_score),
                 "--manager-score", str(manager_score)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            gate2 = report["gates"][1]
            self.assertEqual(gate2["verdict"], "pass")
            self.assertIn("self 90.0, manager 100.0", gate2["reason"])

    def test_bad_manager_score_file_is_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"nope": 1}), encoding="utf-8")
            proc = self.run_gates(
                EXAMPLES / "standard-text.example.json",
                EXAMPLES / "sample-article.txt",
                ["--manager-score", str(bad)])
            self.assertEqual(proc.returncode, 2)
            self.assertIn("score", proc.stderr)


class TestFullFlow(CliMixin, unittest.TestCase):
    def test_full_three_gate_pass_on_sample_article(self):
        proc = self.run_gates(
            EXAMPLES / "standard-text.example.json",
            EXAMPLES / "sample-article.txt",
            ["--manager-standard",
             str(EXAMPLES / "standard-manager.example.json")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["final"], "pass")
        self.assertEqual([g["stage"] for g in report["gates"]],
                         ["self", "manager", "final"])
        self.assertTrue(all(g["verdict"] == "pass" for g in report["gates"]))
        self.assertEqual(report["score"], 100)
        self.assertEqual(report["score_gap"], 0)

    def test_full_three_gate_block_on_short_sample(self):
        proc = self.run_gates(
            EXAMPLES / "standard-text.example.json",
            EXAMPLES / "sample-article-short.txt",
            ["--manager-standard",
             str(EXAMPLES / "standard-manager.example.json")])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertEqual(report["gates"][0]["stage"], "self")
        self.assertEqual(report["gates"][0]["verdict"], "blocked")

    def test_record_file_is_written_and_auditable(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_path = Path(tmp) / "record.json"
            proc = self.run_gates(
                EXAMPLES / "standard-text.example.json",
                EXAMPLES / "sample-article.txt",
                ["--manager-standard",
                 str(EXAMPLES / "standard-manager.example.json"),
                 "--record", str(record_path)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["deliverable"],
                             str(EXAMPLES / "sample-article.txt"))
            self.assertEqual(record["final"], "pass")
            self.assertEqual([g["stage"] for g in record["gates"]],
                             ["self", "manager", "final"])
            for gate in record["gates"]:
                for key in ("stage", "score", "verdict", "reason", "at"):
                    self.assertIn(key, gate)


class TestGate3(unittest.TestCase):
    def test_final_review_blocks_vanished_deliverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            deliverable = Path(tmp) / "draft.txt"
            deliverable.write_text(
                "The Lantern Festival of Gray Harbor\n"
                "The tide carried every lantern home.\n" + "A" * 400,
                encoding="utf-8")
            standard = gates_mod.load_standard(
                str(EXAMPLES / "standard-text.example.json"))
            # Gate 1 passes while the file exists.
            gate1, _ = gates_mod.run_gate1(standard, str(deliverable))
            self.assertEqual(gate1["verdict"], "pass")
            # Gate 2 passes on the gate-1 score (no independent score given).
            gate2, _ = gates_mod.run_gate2(str(deliverable), gate1["score"])
            self.assertEqual(gate2["verdict"], "pass")
            # The deliverable vanishes between gate 2 and gate 3.
            deliverable.unlink()
            gate3 = gates_mod.run_gate3(str(deliverable), [gate1, gate2])
            self.assertEqual(gate3["verdict"], "blocked")
            self.assertIn("does not exist", gate3["reason"])

    def test_final_review_blocks_when_prior_gate_records_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            deliverable = Path(tmp) / "draft.txt"
            deliverable.write_text("Readable text.", encoding="utf-8")
            gate3 = gates_mod.run_gate3(str(deliverable), [])
            self.assertEqual(gate3["verdict"], "blocked")
            self.assertIn("missing passing gate record", gate3["reason"])

    def test_final_review_blocks_unreadable_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            deliverable = Path(tmp) / "blob.txt"
            deliverable.write_bytes(b"\xff\xfe\x00\x01not-text")
            standard = gates_mod.load_standard(
                str(EXAMPLES / "standard-text.example.json"))
            gate1, _ = gates_mod.run_gate1(standard, str(deliverable))
            gate2, _ = gates_mod.run_gate2(str(deliverable), gate1["score"])
            gate3 = gates_mod.run_gate3(str(deliverable), [gate1, gate2])
            self.assertEqual(gate3["verdict"], "blocked")
            self.assertIn("UTF-8", gate3["reason"])


class TestVideoProbe(CliMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clip = Path(cls.tmp.name) / "clip.mp4"
        cls.available = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        if cls.available:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-v", "error",
                 "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=30",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "800k",
                 str(cls.clip)],
                capture_output=True, text=True, timeout=120)
            cls.available = proc.returncode == 0 and cls.clip.exists()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_video_probe_real_clip_passes_all_gates(self):
        if not self.available:
            self.skipTest("ffmpeg/ffprobe not available to generate a test clip")
        proc = self.run_gates(EXAMPLES / "standard-video.example.json",
                              self.clip)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["score"], 100)
        self.assertEqual([g["stage"] for g in report["gates"]],
                         ["self", "manager", "final"])

    def test_video_probe_skipped_when_ffprobe_absent(self):
        if not self.available:
            self.skipTest("ffmpeg/ffprobe not available to generate a test clip")
        with unittest.mock.patch.dict(os.environ, {"PATH": self.tmp.name}):
            result = gates_mod.run_check(
                {"type": "video_probe", "duration": [1, 10],
                 "resolution": [640, 360], "bitrate_kbps": [100, 5000]},
                str(self.clip))
        self.assertTrue(result["skipped"])
        self.assertFalse(result["passed"])
        self.assertIn("ffprobe", result["detail"])

    def test_gate3_blocks_video_when_ffprobe_absent(self):
        if not self.available:
            self.skipTest("ffmpeg/ffprobe not available to generate a test clip")
        gate1 = {"stage": "self", "score": 100, "verdict": "pass",
                 "reason": "ok", "at": "2026-01-01T00:00:00+00:00"}
        gate2 = {"stage": "manager", "score": 100, "verdict": "pass",
                 "reason": "ok", "at": "2026-01-01T00:00:00+00:00"}
        with unittest.mock.patch.dict(os.environ, {"PATH": self.tmp.name}):
            gate3 = gates_mod.run_gate3(str(self.clip), [gate1, gate2])
        self.assertEqual(gate3["verdict"], "blocked")
        self.assertIn("ffprobe", gate3["reason"])


class TestHookAndOutput(CliMixin, unittest.TestCase):
    def test_hook_skipped_when_review_api_url_unset(self):
        proc = self.run_gates(EXAMPLES / "standard-text.example.json",
                              EXAMPLES / "sample-article.txt")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(
            report["second_model_review"],
            "second-model review skipped: REVIEW_API_URL not set")

    def test_hook_error_does_not_change_any_gate_verdict(self):
        # Unreachable localhost address: no real external service is used.
        proc = self.run_gates(
            EXAMPLES / "standard-text.example.json",
            EXAMPLES / "sample-article.txt",
            env_extra={"REVIEW_API_URL": "http://127.0.0.1:1/v1/chat/completions",
                       "REVIEW_API_KEY": "not-a-real-key"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["verdict"], "PASS")
        self.assertTrue(
            report["second_model_review"].startswith(
                "second-model review unavailable:"),
            report["second_model_review"])
        self.assertNotIn("not-a-real-key", proc.stdout + proc.stderr)

    def test_json_output_shape(self):
        proc = self.run_gates(
            EXAMPLES / "standard-text.example.json",
            EXAMPLES / "sample-article.txt",
            ["--manager-standard",
             str(EXAMPLES / "standard-manager.example.json")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        # The review record itself.
        for key in ("deliverable", "standard", "gates", "final"):
            self.assertIn(key, report, "missing record key: %s" % key)
        self.assertIn(report["final"], ("pass", "blocked"))
        self.assertEqual(len(report["gates"]), 3)
        for gate in report["gates"]:
            for key in ("stage", "score", "verdict", "reason", "at"):
                self.assertIn(key, gate, "missing gate key: %s" % key)
            self.assertIn(gate["stage"], ("self", "manager", "final"))
            self.assertIn(gate["verdict"], ("pass", "blocked", "send_back"))
        # Scoring detail carried alongside the record.
        for key in ("verdict", "score", "pass_score", "dimensions",
                    "deductions", "veto", "skipped", "second_model_review"):
            self.assertIn(key, report, "missing detail key: %s" % key)
        for dim in report["dimensions"]:
            for key in ("id", "name", "weight", "earned", "checks"):
                self.assertIn(key, dim)
            for check in dim["checks"]:
                for key in ("type", "passed", "skipped", "detail"):
                    self.assertIn(key, check)

    def test_text_format_is_gate_by_gate_summary(self):
        proc = self.run_gates(
            EXAMPLES / "standard-text.example.json",
            EXAMPLES / "sample-article.txt",
            ["--manager-standard",
             str(EXAMPLES / "standard-manager.example.json")],
            fmt="text")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Final verdict: PASS", proc.stdout)
        self.assertIn("Gate self", proc.stdout)
        self.assertIn("Gate manager", proc.stdout)
        self.assertIn("Gate final", proc.stdout)


if __name__ == "__main__":
    unittest.main()
