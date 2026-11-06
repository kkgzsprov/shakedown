"""A passed gate produces review artifacts without applying game changes."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

import tests  # noqa: F401

from shakedown.orchestrate import digest
from shakedown.proposals import write_proposal


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "tuning_keys.json"
        self.table = {"keys": {"speed": {"type": "float", "min": 1, "max": 5, "default": 3},
                               "count": {"type": "int", "min": 1, "max": 4, "default": 2}}}
        self.source.write_text(json.dumps(self.table, indent=2) + "\n", encoding="utf-8")
        self.original = self.source.read_bytes()
        self.source_hash = hashlib.sha256(self.original).hexdigest()
        self.config = SimpleNamespace(path=self.root / "shakedown.toml", root=self.root,
                                      raw={"tuning": {"keys": "tuning_keys.json"}})
        self.candidate = {"speed": 5, "count": 2}
        self.output = self.root / "proposal"

    def propose(self, *, candidate=None, gate=None, source_hash=None):
        chosen = self.candidate if candidate is None else candidate
        return write_proposal(self.config, chosen, self.output,
            gate={"status": "passed", "proposed": True, "candidate_hash": digest(chosen),
                  "baseline_hash": digest({"speed": 3.0, "count": 2})} if gate is None else gate,
            source_sha256=self.source_hash if source_hash is None else source_hash)

    def test_reviewable_patch_and_values_leave_source_unchanged(self):
        original_candidate = deepcopy(self.candidate)
        result = self.propose()
        self.assertEqual(result["status"], "proposed")
        self.assertFalse(result["applied"])
        self.assertEqual(result["source_sha256"], self.source_hash)
        self.assertEqual(result["changes"], [{"key": "speed", "field": "default", "before": 3, "after": 5}])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.candidate, original_candidate)
        values = json.loads((self.output / "tuning.json").read_text(encoding="utf-8"))
        self.assertEqual(values, {"tuning_version": 1, "values": self.candidate})
        patch = (self.output / "defaults.patch").read_text(encoding="utf-8")
        self.assertIn("--- a/tuning_keys.json", patch)
        self.assertIn("+++ b/tuning_keys.json", patch)
        checked = subprocess.run(["git", "apply", "--check", str(self.output / "defaults.patch")],
                                 cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_failed_or_unmeasured_gate_never_creates_artifacts(self):
        for gate in ({"status": "rejected", "proposed": False},
                     {"status": "unmeasured", "proposed": False},
                     {"status": "passed", "proposed": False}):
            with self.subTest(gate=gate), self.assertRaises(ValueError):
                self.propose(gate=gate)
            self.assertFalse(self.output.exists())

    def test_stale_source_hash_and_outside_root_are_rejected_before_output(self):
        with self.assertRaisesRegex(ValueError, "changed"):
            self.propose(source_hash="0" * 64)
        self.assertFalse(self.output.exists())
        self.config.root = self.root / "different-root"
        with self.assertRaisesRegex(ValueError, "inside"):
            self.propose()
        self.assertFalse(self.output.exists())

    def test_gate_must_match_the_candidate_and_documented_baseline(self):
        valid = {"status": "passed", "proposed": True, "candidate_hash": digest(self.candidate),
                 "baseline_hash": digest({"speed": 3.0, "count": 2})}
        for field in ("candidate_hash", "baseline_hash"):
            gate = {**valid, field: "0" * 64}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.propose(gate=gate)
            self.assertFalse(self.output.exists())

    def test_source_without_final_newline_still_produces_an_applicable_patch(self):
        self.source.write_bytes(self.original.rstrip(b"\n"))
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.propose()
        checked = subprocess.run(["git", "apply", "--check", str(self.output / "defaults.patch")],
                                 cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_unchanged_incomplete_unknown_and_invalid_tunings_fail_before_output(self):
        for candidate in ({"speed": 3, "count": 2}, {"speed": 5},
                          {"speed": 5, "count": 2, "other": 1},
                          {"speed": 6, "count": 2}, {"speed": True, "count": 2},
                          {"speed": 5, "count": 2.5}, {"speed": float("inf"), "count": 2}):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.propose(candidate=candidate)
            self.assertFalse(self.output.exists())
            self.assertEqual(self.source.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
