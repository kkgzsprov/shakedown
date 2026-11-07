"""`apply` writes only a verified, unapplied passed-gate proposal."""

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import tests  # noqa: F401

from shakedown.apply import apply_proposal
from shakedown.cli import main
from shakedown.orchestrate import digest, write_json
from shakedown.proposals import write_proposal


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "tuning_keys.json"
        self.table = {"keys": {"speed": {"type": "float", "min": 1, "max": 5, "default": 3},
                               "count": {"type": "int", "min": 1, "max": 4, "default": 2}}}
        self.source.write_text(json.dumps(self.table, indent=2) + "\n", encoding="utf-8")
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.config = SimpleNamespace(path=self.root / "shakedown.toml", root=self.root,
                                      raw={"tuning": {"keys": "tuning_keys.json"}})
        self.candidate = {"speed": 5, "count": 2}
        self.output = self.root / "proposal"
        self.receipt = write_proposal(self.config, self.candidate, self.output,
            gate={"status": "passed", "proposed": True, "candidate_hash": digest(self.candidate),
                  "baseline_hash": digest({"speed": 3.0, "count": 2})},
            source_sha256=self.source_hash)

    def proposal_json(self):
        return json.loads((self.output / "proposal.json").read_text(encoding="utf-8"))

    def test_verify_only_leaves_table_and_receipt_unchanged(self):
        original = self.source.read_bytes()
        result = apply_proposal(self.config, self.output)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["changes"], self.receipt["changes"])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.proposal_json(), self.receipt)

    def test_approved_apply_rewrites_table_and_receipt_then_refuses_again(self):
        result = apply_proposal(self.config, self.output, approved=True)
        self.assertEqual(result["status"], "applied")
        table = json.loads(self.source.read_text(encoding="utf-8"))
        self.assertEqual(table["keys"]["speed"]["default"], 5)
        self.assertEqual(table["keys"]["count"]["default"], 2)
        self.assertNotEqual(result["applied_sha256"], self.source_hash)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),
                         result["applied_sha256"])
        receipt = self.proposal_json()
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["application"]["before_sha256"], self.source_hash)
        self.assertEqual(receipt["application"]["after_sha256"], result["applied_sha256"])
        self.assertEqual(receipt["application"]["changes"], self.receipt["changes"])
        with self.assertRaisesRegex(ValueError, "unapplied"):
            apply_proposal(self.config, self.output, approved=True)

    def test_stale_source_is_refused_before_any_write(self):
        self.source.write_text(json.dumps(
            {"keys": {"speed": {"type": "float", "min": 1, "max": 5, "default": 4},
                      "count": {"type": "int", "min": 1, "max": 4, "default": 2}}}) + "\n",
            encoding="utf-8")
        current = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed"):
            apply_proposal(self.config, self.output, approved=True)
        self.assertEqual(self.source.read_bytes(), current)
        self.assertFalse(self.proposal_json()["applied"])

    def test_tampered_artifacts_are_refused(self):
        gate = {"status": "passed", "proposed": True,
                "candidate_hash": digest(self.candidate),
                "baseline_hash": digest({"speed": 3.0, "count": 2})}
        original = self.source.read_bytes()
        for label in ("patch", "values", "receipt"):
            with self.subTest(artifact=label):
                proposal_dir = self.root / f"proposal-{label}"
                write_proposal(self.config, self.candidate, proposal_dir,
                               gate=gate, source_sha256=self.source_hash)
                if label == "patch":
                    (proposal_dir / "defaults.patch").write_text("# forged\n", encoding="utf-8")
                elif label == "values":
                    write_json(proposal_dir / "tuning.json",
                        {"tuning_version": 1, "values": {**self.candidate, "speed": 4}})
                else:
                    write_json(proposal_dir / "proposal.json", {**self.receipt, "changes": []})
                with self.assertRaises(ValueError):
                    apply_proposal(self.config, proposal_dir, approved=True)
        self.assertEqual(self.source.read_bytes(), original)

    def test_outside_root_and_wrong_table_are_refused(self):
        receipt = {**self.receipt, "source": "../outside.json"}
        write_json(self.output / "proposal.json", receipt)
        with self.assertRaisesRegex(ValueError, "inside"):
            apply_proposal(self.config, self.output)
        write_json(self.output / "proposal.json", self.receipt)
        self.config.raw["tuning"]["keys"] = "other_keys.json"
        with self.assertRaisesRegex(ValueError, "configured tuning key table"):
            apply_proposal(self.config, self.output)

    def test_cli_verifies_and_applies_with_explicit_approval(self):
        config_path = self.root / "shakedown.toml"
        config_path.write_text(
            '[game]\ncwd = "."\ncommand = ["unused-game"]\n'
            "[batch]\nseeds = [1]\n[tuning]\nkeys = \"tuning_keys.json\"\n",
            encoding="utf-8")
        argv = ["apply", "--config", str(config_path), "--proposal", str(self.output)]
        self.assertEqual(main(argv), 0)
        self.assertEqual(json.loads(self.source.read_text(encoding="utf-8")),
                         self.table)
        self.assertEqual(main(argv + ["--approved"]), 0)
        self.assertEqual(json.loads(self.source.read_text(encoding="utf-8"))["keys"]["speed"]["default"], 5)
        self.assertTrue(self.proposal_json()["applied"])


if __name__ == "__main__":
    unittest.main()
