"""Public QA commands reject bad settings before consuming game budgets."""

import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "examples/tinyraid/shakedown.toml"


class QaCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.environment = dict(os.environ)
        self.environment["PYTHONPATH"] = str(ROOT / "src")

    def command(self, *arguments):
        return subprocess.run([sys.executable, "-m", "shakedown", *map(str, arguments)],
                              cwd=ROOT, env=self.environment, capture_output=True,
                              text=True, timeout=30)

    def test_zero_deadline_records_skipped_work_and_supports_empty_triage(self):
        batch = self.base / "exploration"
        result = self.command("explore", "--config", CONFIG, "--output", batch,
                              "--runs", "6", "--deadline", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((batch / "exploration.json").read_text())
        self.assertEqual(manifest["total_runs"], 0)
        self.assertEqual(len(manifest["skipped_runs"]), 6)
        self.assertTrue((batch / "anomalies.md").is_file())
        analysis = self.command("anomalies", "--config", CONFIG, "--batch", batch)
        self.assertEqual(analysis.returncode, 0, analysis.stderr)
        triage = self.command("triage", "--config", CONFIG, "--batch", batch,
                              "--output", self.base / "catalog")
        self.assertEqual(triage.returncode, 0, triage.stderr)
        index = json.loads((self.base / "catalog/findings/index.json").read_text())
        self.assertEqual(index["entries"], [])

    def test_bad_anomaly_settings_do_not_launch_run_or_explore(self):
        original = CONFIG.read_text(encoding="utf-8")
        original = original.replace('cwd = "../.."', "cwd = " + json.dumps(str(ROOT)))
        original = original.replace('keys = "tuning_keys.json"', "keys = " + json.dumps(
            str(ROOT / "examples/tinyraid/tuning_keys.json")))
        changes = {
            "boolean-count": "min_runs = true",
            "bad-group": "min_runs = 5\ngroup_by = 1",
            "unknown-setting": "min_runs = 5\nmin_runz = 1",
            "bad-threshold": "min_runs = 5\nthresholds = {timeout = 2}",
        }
        for name, setting in changes.items():
            config = self.base / f"{name}.toml"
            config.write_text(original.replace("min_runs = 5", setting), encoding="utf-8")
            for command in ("run", "explore"):
                with self.subTest(setting=name, command=command):
                    output = self.base / f"{name}-{command}"
                    result = self.command(command, "--config", config, "--output", output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse(output.exists())

    def test_benchmark_rejects_unknown_case_before_creating_evidence(self):
        output = self.base / "unknown"
        result = self.command("bench", "tinyraid", "--output", output, "--cases", "not-a-case")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(output.exists())

    def test_full_protocol_eligibility_does_not_hide_a_failed_benchmark_gate(self):
        from shakedown.cli import main
        with patch("shakedown.bench.bench_tinyraid", return_value={
            "qualified": True, "gate_passed": False, "status": "failed"
        }), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["bench", "tinyraid", "--output", str(self.base / "failed")]), 1)

    def test_interrupted_benchmark_has_the_cancellation_exit_code(self):
        from shakedown.cli import main
        with patch("shakedown.bench.bench_tinyraid", return_value={
            "qualified": False, "gate_passed": False, "status": "interrupted"
        }), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["bench", "tinyraid", "--output", str(self.base / "cancelled")]), 130)


if __name__ == "__main__":
    unittest.main()
