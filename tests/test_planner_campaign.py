"""Approved finite campaign dimensions and execution receipts."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import tests  # noqa: F401

from shakedown.config import load_config
from shakedown.planner.campaign import run_approved_campaign


ROOT = Path(__file__).resolve().parents[1]


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shakedown-campaign-")
        self.addCleanup(tests.cleanup_temp_directory, self.temp)
        self.base = Path(self.temp.name)
        self.config = load_config(ROOT / "examples/tinyraid/shakedown.toml")
        self.document = {"rounds": 1, "batch_size": 1, "search_ranges": {},
                         "profile_instructions": {"cautious": "Approved cautious behavior",
                                                  "looter": "Approved loot behavior"},
                         "experiments": [{"id": "first", "profiles": ["cautious", "looter"],
                                          "maps": ["yard"], "seeds": [7, 9],
                                          "tuning_ranges": {"player.speed": {"min": 2, "max": 4}}}]}
        self.targets = self.base / "targets.json"
        self.targets.write_text('{"schema_version":1}', encoding="utf-8")
        self.gate = mock.Mock(side_effect=lambda *args: deepcopy(self.document))
        self.module = SimpleNamespace(approved_campaign=self.gate)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(sys.modules, {"shakedown.planner.engine": self.module}).start()
        self.output = self.base / "output"

    def run_campaign(self, **kwargs):
        return run_approved_campaign(self.config, self.base / "campaign.json", self.base / "approval.json",
                                     self.output, targets=self.targets,
                                     targets_approval=self.base / "targets-approval.json", **kwargs)

    @staticmethod
    def completed(config, output):
        return {"status": "completed", "valid_runs": 1, "invalid_runs": 0,
                "runs": [{"id": "r000001", "profile": config.profiles[0], "valid": True}]}

    def test_dimensions_instructions_and_candidate_objects_are_preserved(self):
        original = self.config
        with mock.patch("shakedown.planner.campaign.run_batch", side_effect=self.completed) as batch:
            result = self.run_campaign(approved=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_runs"], 2)
        self.assertEqual(len(result["runs"]), 2)
        for index, call in enumerate(batch.call_args_list):
            derived, path = call.args
            profile = ("cautious", "looter")[index]
            self.assertEqual(derived.maps, ("yard",))
            self.assertEqual(derived.profiles, (profile,))
            self.assertEqual(derived.seeds, (7,))
            self.assertEqual(derived.instructions, self.document["profile_instructions"][profile])
            self.assertIs(derived.tuning_candidates, original.tuning_candidates)
            self.assertEqual(path, self.output.resolve() / "e0001" / f"p{index + 1:04d}")
        self.assertEqual(self.config, original)
        self.assertEqual(json.loads((self.output / "campaign_run.json").read_text()), result)
        self.assertEqual(len(result["campaign_sha256"]), 64)

    def test_explicit_approval_and_receipt_gate_precede_output(self):
        with mock.patch("shakedown.planner.campaign.run_batch") as batch:
            with self.assertRaisesRegex(ValueError, "explicit"):
                self.run_campaign()
            self.gate.assert_not_called()
            self.gate.side_effect = ValueError("approval mismatch")
            with self.assertRaisesRegex(ValueError, "approval mismatch"):
                self.run_campaign(approved=True)
            batch.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_cap_counts_all_dimensions_before_children(self):
        self.config = replace(self.config, backends=("rule", "mock"), tuning_candidates=({}, {"player.speed": 3.5}))
        self.document["batch_size"] = 2
        self.document["experiments"][0]["maps"] = ["yard", "warehouse"]
        with mock.patch("shakedown.planner.campaign.run_batch") as batch:
            with self.assertRaisesRegex(ValueError, "32 runs"):
                self.run_campaign(approved=True, max_runs=31)
            batch.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_unsupported_rounds_and_ranges_fail_without_output(self):
        for mode in ("rounds", "range", "partial", "missing_default"):
            with self.subTest(mode=mode):
                self.set_document(mode)
                with mock.patch("shakedown.planner.campaign.run_batch") as batch:
                    with self.assertRaises(ValueError):
                        self.run_campaign(approved=True)
                    batch.assert_not_called()
                self.assertFalse(self.output.exists())

    def set_document(self, mode):
        self.document["rounds"] = 2 if mode == "rounds" else 1
        self.config = replace(self.config, tuning_candidates=({"player.speed": 3},))
        self.document["experiments"][0]["tuning_ranges"] = {"player.speed": {"min": 2, "max": 4}}
        if mode == "range":
            self.document["experiments"][0]["tuning_ranges"]["player.speed"]["max"] = 2.5
        elif mode == "partial":
            self.document["experiments"][0]["tuning_ranges"] = {}
        elif mode == "missing_default":
            table = self.base / "keys.json"
            table.write_text('{"keys":{"player.speed":{"type":"float","min":1,"max":5}}}')
            self.config = replace(self.config, path=self.base / "config.toml", tuning_candidates=({},),
                                  raw={"tuning": {"keys": "keys.json"}})

    def test_failure_and_interruption_are_persisted_and_stop_remaining_batches(self):
        for error, status in ((RuntimeError("child failed"), "failed"), (KeyboardInterrupt(), "interrupted")):
            with self.subTest(status=status):
                self.output = self.base / status
                with mock.patch("shakedown.planner.campaign.run_batch", side_effect=error) as batch:
                    with self.assertRaises(type(error)):
                        self.run_campaign(approved=True)
                    self.assertEqual(batch.call_count, 1)
                receipt = json.loads((self.output / "campaign_run.json").read_text())
                self.assertEqual(receipt["status"], status)
                self.assertEqual(receipt["batches"][0]["status"], status)
                self.assertEqual(receipt["runs"], [])

    def test_returned_interrupted_batch_stops_campaign(self):
        manifest = {"status": "interrupted", "runs": [{"id": "r1", "valid": False}], "invalid_runs": 1}
        with mock.patch("shakedown.planner.campaign.run_batch", return_value=manifest) as batch:
            result = self.run_campaign(approved=True)
            self.assertEqual(batch.call_count, 1)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(len(result["runs"]), 1)

    def test_one_real_tinyraid_run(self):
        self.config = replace(self.config, duration=.1, workers=1, bootstrap_samples=10)
        self.document["experiments"][0]["profiles"] = ["cautious"]
        result = self.run_campaign(approved=True, max_runs=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["runs"]), 1)
        self.assertTrue(result["runs"][0]["valid"])
        run = self.output / "e0001/p0001/runs/r000001/run.json"
        self.assertTrue(run.is_file())


if __name__ == "__main__":
    unittest.main()
