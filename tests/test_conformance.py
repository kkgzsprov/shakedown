"""Adapter verdicts require observed evidence, including game-owned fixtures."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

import tests  # noqa: F401 - Make the source package available without installing.

from shakedown.config import load_config
from shakedown.conformance import _run_process, check_adapter


ROOT = Path(__file__).resolve().parents[1]


class ConformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = replace(load_config(ROOT / "examples/tinyraid/shakedown.toml"), duration=3)

    def test_real_tinyraid_reports_all_seven_checks_and_keeps_evidence(self):
        destination = self.base / "all"
        report = check_adapter(self.config, destination)
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(set(report["checks"]), {f"C{i}" for i in range(1, 8)})
        for name, verdict in report["checks"].items():
            with self.subTest(check=name):
                self.assertEqual(verdict["status"], "passed", verdict)
                self.assertTrue(verdict["evidence"])
                for evidence in verdict["evidence"]:
                    self.assertTrue((destination / evidence).exists())
        stored = json.loads((destination / "conformance.json").read_text(encoding="utf-8"))
        self.assertEqual(report, stored)
        self.assertEqual(report["checks"]["C3"]["decision_counts"][0],
                         report["checks"]["C3"]["decision_counts"][2])

    def test_contract_probes_use_the_same_strict_tuning_envelope_as_normal_runs(self):
        wrapper = self.base / "strict_tuning.py"
        wrapper.write_text("""
import json, sys
from pathlib import Path
from examples.tinyraid.__main__ import main
if '--tuning' in sys.argv:
    path = Path(sys.argv[sys.argv.index('--tuning') + 1])
    tuning = json.loads(path.read_text(encoding='utf-8'))
    if (type(tuning) is not dict or set(tuning) != {'tuning_version', 'values'}
            or type(tuning['tuning_version']) is not int or tuning['tuning_version'] != 1
            or type(tuning['values']) is not dict):
        print('strict adapter requires the versioned tuning envelope', file=sys.stderr)
        sys.exit(2)
sys.exit(main())
""", encoding="utf-8")
        config = replace(self.config, command=["{python}", str(wrapper), *self.config.command[3:]])
        destination = self.base / "strict-tuning"
        report = check_adapter(config, destination)
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(report["checks"]["C4"]["status"], "passed")
        for fault in ("seq", "choice", "json"):
            receipt = json.loads((destination / f"c4-{fault}/check_process.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt["answered"], receipt)
            tuning = json.loads((destination / f"c4-{fault}-tuning.json").read_text(encoding="utf-8"))
            self.assertEqual(tuning, {"tuning_version": 1, "values": {}})

    def test_oracle_replay_must_reproduce_the_configured_oracle_kind(self):
        wrapper = self.base / "wrong_oracle.py"
        wrapper.write_text("""
import sys
from examples.tinyraid.game import Game
original_setup = Game._scenario_setup
def setup(game):
    original_setup(game)
    if game.scenario == 'stuck':
        game.player.hp = 125
Game._scenario_setup = setup
from examples.tinyraid.__main__ import main
sys.exit(main())
""", encoding="utf-8")
        wrapped = replace(self.config, command=["{python}", str(wrapper), *self.config.command[3:]],
                          replay_command=["{python}", str(wrapper), *self.config.replay_command[3:]])
        for expected, status in (("stuck", "failed"), ("numeric", "passed")):
            with self.subTest(expected=expected):
                checks = dict(wrapped.checks)
                if expected == "stuck":
                    checks.pop("oracle_kind", None)  # The default contract is specifically stuck.
                else:
                    checks["oracle_kind"] = expected
                report = check_adapter(replace(wrapped, checks=checks), self.base / f"oracle-{expected}")
                self.assertEqual(report["status"], status, report)
                oracle = report["checks"]["C6"]
                self.assertEqual(oracle["status"], status, oracle)
                self.assertEqual(oracle["expected_oracle"], expected)
                self.assertEqual(oracle["observed_oracles"], ["numeric", "numeric"])
                self.assertTrue(oracle["identical"])
                for name, verdict in report["checks"].items():
                    if name != "C6":
                        self.assertEqual(verdict["status"], "passed", (name, verdict))

    def test_missing_custom_fixtures_are_unmeasured_not_passed(self):
        config = replace(self.config, checks={})
        report = check_adapter(config, self.base / "missing")
        self.assertEqual(report["status"], "unmeasured", report)
        self.assertEqual(report["checks"]["C6"]["status"], "unmeasured")
        self.assertEqual(report["checks"]["C7"]["status"], "unmeasured")
        self.assertEqual(report["checks"]["C1"]["status"], "passed")

    def test_no_replay_command_leaves_input_and_oracle_replay_unmeasured(self):
        config = replace(self.config, replay_command=None)
        report = check_adapter(config, self.base / "no-replay")
        self.assertEqual(report["status"], "unmeasured", report)
        for name in ("C2", "C6"):
            self.assertEqual(report["checks"][name]["status"], "unmeasured")

    def test_visibility_leak_is_failed_even_when_other_checks_pass(self):
        config = replace(self.config, checks={**self.config.checks,
            "visibility_command": ["{python}", "-m", "examples.tinyraid.checks",
                                   "--bug", "vision_leak"]})
        report = check_adapter(config, self.base / "leak")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["checks"]["C7"]["status"], "failed")
        self.assertEqual(report["checks"]["C7"]["fixture"]["leaked_ids"], ["g1"])

    def test_visibility_fixture_needs_explicit_true_not_just_zero_exit(self):
        config = replace(self.config, checks={**self.config.checks,
            "visibility_command": ["{python}", "-c", "print('zero exit has no verdict')"]})
        report = check_adapter(config, self.base / "no-verdict")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["checks"]["C7"]["status"], "failed")

    def test_existing_output_is_not_overwritten(self):
        destination = self.base / "existing"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("preserve", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            check_adapter(self.config, destination)
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_bad_answer_probe_wait_is_bounded_and_keeps_timeout_evidence(self):
        script = """
import json, sys, time
request = {'v':1,'type':'decide','seq':3,'step':0,'reason':'start',
           'profile':'test','state':{},'candidates':{'hold':'Hold'},'order_seed':1}
print('@@shakedown ' + json.dumps(request), flush=True)
sys.stdin.readline()
time.sleep(60)
"""
        config = replace(self.config, wall_timeout=.5)
        started = time.monotonic()
        result = _run_process(config, [sys.executable, "-c", script],
                              self.base / "hung", "choice")
        self.assertTrue(result["wall_timeout"])
        self.assertTrue(result["answered"])
        self.assertLess(time.monotonic() - started, 8)
        evidence = json.loads((self.base / "hung/check_process.json").read_text(encoding="utf-8"))
        self.assertTrue(evidence["wall_timeout"])

    def test_a_second_decision_after_bad_answer_does_not_count_as_rejection(self):
        script = """
import json, sys
request = {'v':1,'type':'decide','seq':3,'step':0,'reason':'start',
           'profile':'test','state':{},'candidates':{'hold':'Hold'},'order_seed':1}
for seq in (3, 4):
    request['seq'] = seq
    print('@@shakedown ' + json.dumps(request), flush=True)
    if seq == 3:
        sys.stdin.readline()
sys.exit(2)
"""
        result = _run_process(self.config, [sys.executable, "-c", script],
                              self.base / "continued", "json")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["later_requests"], 1)


class VisibilityFixtureTests(unittest.TestCase):
    def test_hidden_entity_and_positive_control_are_both_checked(self):
        from examples.tinyraid.checks import check_visibility

        clean, leaked = check_visibility(), check_visibility("vision_leak")
        self.assertTrue(clean["passed"])
        self.assertTrue(clean["positive_control_seen"])
        self.assertEqual(clean["leaked_ids"], [])
        self.assertFalse(leaked["passed"])
        self.assertEqual(leaked["leaked_ids"], ["g1"])


if __name__ == "__main__":
    unittest.main()
