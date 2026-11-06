"""Approved numeric search cannot learn from or rerank using its holdout."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from shakedown.campaign import run_balance_campaign
from shakedown.config import load_config
from shakedown.orchestrate import digest, write_json
from shakedown.planner.engine import approve_plan, run_step as real_run_step


class BalanceCampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "policy.py").write_text("# Frozen game and adapter fixture\n", encoding="utf-8")
        self.source = self.root / "tuning_keys.json"
        write_json(self.source, {"keys": {"speed": {"type": "float", "min": 1, "max": 5, "default": 3}}})
        self.config_path = self.root / "shakedown.toml"
        self.config = self.configure()
        self.targets = self.root / "targets.json"
        self.targets_approval = self.root / "targets-approved.json"
        self.campaign = self.root / "campaign.json"
        self.approval = self.root / "campaign-approved.json"
        self.output = self.root / "output"
        write_json(self.targets, {"schema_version": 1, "targets": [{"metric": "loot", "profile": "cautious",
            "map": "yard", "min": 40, "max": 60, "rationale": "Planted measurement target"}], "notes": "Fixture"})
        self.document = {"schema_version": 1, "hypotheses": ["Higher speed increases reward."],
            "experiments": [{"id": "baseline", "hypothesis": "Compare a frozen policy.",
                "profiles": ["cautious"], "maps": ["yard"], "seeds": [11, 13],
                "tuning_ranges": {"speed": {"min": 1, "max": 5}}}],
            "profile_instructions": {"cautious": "Approved instructions only."},
            "search_ranges": {"speed": {"min": 1, "max": 5}}, "batch_size": 2,
            "rounds": 2, "stop_conditions": ["Stop at the approved round cap."]}
        self.calls = []
        self.loot_function = lambda tuning, held_out: tuning["speed"] * 10
        self.approve()

    def configure(self, **overrides):
        settings = {"method": "grid", "seed": 0, "candidates_per_round": 2, "grid_points": 5,
                    "holdout_seeds": [101, 103], "min_runs": 2, "min_gain": .2, "top_k": 1,
                    "max_runs": 100, "policies": ["rule", "mock"], "planner_revisions": False,
                    "policy_files": ["policy.py"]}
        settings.update(overrides)
        lines = ['[game]', 'cwd = "."', 'command = ["unused-game"]', 'maps = ["yard"]',
                 'profiles = ["cautious"]', '[batch]', 'seeds = [11, 13, 17]', 'deciders = ["rule"]',
                 'seconds = 1', '[tuning]', 'keys = "tuning_keys.json"', '[planner]', 'backend = "mock"',
                 '[balance]']
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in settings.items())
        self.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return load_config(self.config_path)

    def approve(self):
        write_json(self.campaign, self.document)
        for path in (self.targets_approval, self.approval):
            if path.exists():
                path.unlink()
        approve_plan(self.config, "targets", self.targets, self.targets_approval, approved=True)
        approve_plan(self.config, "campaign", self.campaign, self.approval, approved=True,
                     targets=self.targets, targets_approval=self.targets_approval)

    def run_campaign(self, **kwargs):
        return run_balance_campaign(self.config, self.campaign, self.approval, self.output,
            targets=self.targets, targets_approval=self.targets_approval, **kwargs)

    @staticmethod
    def estimate(value, n):
        return {"mean": value, "ci95": [value, value], "n": n}

    def batch(self, config, output):
        relative = output.relative_to(self.output).as_posix()
        held_out = relative.startswith("holdout/")
        entry = {"relative": relative, "config": config}
        if held_out:
            entry["selection"] = (self.output / "selection.json").read_bytes()
            self.assertEqual(tuple(config.seeds), (101, 103))
        else:
            self.assertTrue(set(config.seeds) <= {11, 13, 17})
            self.assertFalse(set(config.seeds) & {101, 103})
        self.calls.append(entry)
        groups = []
        for tuning in config.tuning_candidates:
            for policy in config.backends:
                groups.append({"id": f"g{len(groups)+1:04d}", "profile": config.profiles[0],
                    "map": config.maps[0], "decider": policy, "tuning_hash": digest(tuning),
                    "runs": len(config.seeds), "metrics": {
                        "loot": self.estimate(self.loot_function(tuning, held_out), len(config.seeds)),
                        "oracle_rate": self.estimate(0, len(config.seeds))}})
        output.mkdir(parents=True)
        write_json(output / "metrics.json", {"v": 1, "groups": groups, "invalid_runs": []})
        return {"status": "completed", "valid_runs": sum(group["runs"] for group in groups),
                "invalid_runs": 0, "runs": []}

    def patches(self):
        return patch("shakedown.campaign.run_batch", side_effect=self.batch), patch(
            "shakedown.campaign.analyze_batch", return_value={"findings": []})

    def test_search_rounds_freeze_selection_before_holdout_and_never_apply_defaults(self):
        original = self.source.read_bytes()
        batch_patch, anomaly_patch = self.patches()
        with batch_patch, anomaly_patch:
            result = self.run_campaign(approved=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["holdout_used"])
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual([call["relative"] for call in self.calls], ["search/baseline/c0001",
            "search/round-001/c0001", "search/round-002/c0001", "holdout/c0001"])
        self.assertEqual(result["attempted_runs"], 28)
        for call in self.calls:
            self.assertEqual(call["config"].instructions, "Approved instructions only.")
            self.assertEqual(call["config"].backends, ("rule", "mock"))
            self.assertEqual(call["config"].duration, self.config.duration)
        selection = (self.output / "selection.json").read_bytes()
        self.assertEqual(self.calls[-1]["selection"], selection)
        self.assertEqual(json.loads(selection)["selection_source"], "search_only")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(len(result["proposals"]), 1)
        proposed = self.output / result["proposals"][0]["path"] / "proposal.json"
        self.assertFalse(json.loads(proposed.read_text(encoding="utf-8"))["applied"])
        self.assertTrue((self.output / "report.md").is_file())
        self.assertTrue((self.output / "planner-report/report.json").is_file())

    def test_experiment_scopes_union_unique_search_seeds(self):
        second = deepcopy(self.document["experiments"][0])
        second.update(id="second", seeds=[13, 17])
        self.document["experiments"].append(second)
        self.approve()
        batch_patch, anomaly_patch = self.patches()
        with batch_patch, anomaly_patch:
            result = self.run_campaign(approved=True)
        self.assertEqual(result["search_seeds"], [11, 13, 17])
        self.assertEqual(self.calls[0]["config"].seeds, (11, 13, 17))
        self.assertEqual(len({call["relative"] for call in self.calls}), 4)

    def test_no_search_improvement_does_not_touch_holdout(self):
        self.loot_function = lambda tuning, held_out: 30
        batch_patch, anomaly_patch = self.patches()
        with batch_patch, anomaly_patch:
            result = self.run_campaign(approved=True)
        self.assertFalse(result["holdout_used"])
        self.assertEqual(result["selected"], [])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["stop_reason"], "no_measured_search_improvement")
        self.assertFalse((self.output / "holdout").exists())

    def test_holdout_regression_cannot_rerank_the_frozen_search_selection(self):
        self.loot_function = lambda tuning, held_out: 0 if held_out and tuning["speed"] != 3 else tuning["speed"] * 10
        batch_patch, anomaly_patch = self.patches()
        with batch_patch, anomaly_patch:
            result = self.run_campaign(approved=True)
        self.assertTrue(result["holdout_used"])
        self.assertEqual(len(result["selected"]), 1)
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["gates"][0]["status"], "rejected")
        frozen = json.loads(self.calls[-1]["selection"])
        self.assertEqual(frozen["selected"], result["selected"])

    def test_explicit_approval_caps_stale_inputs_and_overlap_fail_before_children(self):
        with patch("shakedown.campaign.run_batch") as batch:
            for kwargs in ({}, {"approved": True, "max_runs": 1}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    self.run_campaign(**kwargs)
                self.assertFalse(self.output.exists())
            self.config = self.configure(holdout_seeds=[11, 101])
            self.approve()
            with self.assertRaisesRegex(ValueError, "disjoint"):
                self.run_campaign(approved=True)
            self.assertFalse(self.output.exists())
            self.config = self.configure()
            self.approve()
            self.document["hypotheses"].append("Unapproved mutation")
            write_json(self.campaign, self.document)
            with self.assertRaises(ValueError):
                self.run_campaign(approved=True)
            self.assertFalse(self.output.exists())
            batch.assert_not_called()

    def test_planner_revision_clips_to_approved_ranges_and_can_stop_search(self):
        self.config = self.configure(planner_revisions=True, candidates_per_round=3)
        self.document["search_ranges"] = {"speed": {"min": 2, "max": 4}}
        self.document["experiments"][0]["tuning_ranges"] = deepcopy(self.document["search_ranges"])
        self.approve()
        def planner(config, step, output, **kwargs):
            if step != "revise":
                return real_run_step(config, step, output, **kwargs)
            path = output / "revision.json"
            path.parent.mkdir(parents=True)
            write_json(path, {"schema_version": 1, "hypotheses": [],
                "search_ranges": {"speed": {"min": 1, "max": 5}},
                "decision": "stop", "reason": "Fixture stop", "clipping": []})
            return {"artifact": str(path)}
        batch_patch, anomaly_patch = self.patches()
        with batch_patch, anomaly_patch, patch("shakedown.campaign.run_step", side_effect=planner):
            result = self.run_campaign(approved=True)
        self.assertEqual(len(result["rounds"]), 1)
        self.assertEqual(result["stop_reason"], "revision_stop")
        revision = json.loads((self.output / result["rounds"][0]["revision"]).read_text(encoding="utf-8"))
        self.assertEqual(revision["search_ranges"], {"speed": {"min": 2, "max": 4}})
        self.assertEqual(revision["clipping"][0]["scope"], "approved_campaign")
        self.assertNotIn("search/round-002/c0001", [call["relative"] for call in self.calls])

    def test_invalid_and_interrupted_batches_keep_failed_receipts_without_holdout(self):
        for status, invalid, expected, exception in (("completed", 1, "failed", ValueError),
                                                    ("interrupted", 0, "interrupted", KeyboardInterrupt)):
            self.output = self.root / status
            with self.subTest(status=status), patch("shakedown.campaign.run_batch", return_value={
                    "status": status, "valid_runs": 0, "invalid_runs": invalid, "runs": []}):
                with self.assertRaises(exception):
                    self.run_campaign(approved=True)
            receipt = json.loads((self.output / "balance_run.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], expected)
            self.assertFalse(receipt["holdout_used"])
            self.assertEqual(receipt["proposals"], [])
            self.assertTrue((self.output / "report.md").is_file())

    def test_changed_policy_inputs_stop_before_next_batch(self):
        def changed(config, output):
            result = self.batch(config, output)
            (self.root / "policy.py").write_text("# Changed executor policy\n", encoding="utf-8")
            return result
        with patch("shakedown.campaign.run_batch", side_effect=changed) as batch, patch(
                "shakedown.campaign.analyze_batch", return_value={"findings": []}):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.run_campaign(approved=True)
        self.assertEqual(batch.call_count, 1)
        result = json.loads((self.output / "balance_run.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["holdout_used"])

    def test_policy_file_outside_project_is_rejected_before_children(self):
        self.config = self.configure(policy_files=["../outside-policy.py"])
        self.approve()
        with patch("shakedown.campaign.run_batch") as batch, self.assertRaisesRegex(ValueError, "inside"):
            self.run_campaign(approved=True)
        batch.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_policy_change_during_supplemental_report_invalidates_proposals(self):
        def changed(config, step, output, **kwargs):
            result = real_run_step(config, step, output, **kwargs)
            if step == "report":
                (self.root / "policy.py").write_text("# Changed during report\n", encoding="utf-8")
            return result
        batch_patch, anomaly_patch = self.patches()
        with batch_patch, anomaly_patch, patch("shakedown.campaign.run_step", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.run_campaign(approved=True)
        receipt = json.loads((self.output / "balance_run.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(len(receipt["proposals"]), 1)
        path = self.output / receipt["proposals"][0]["path"] / "proposal.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "invalidated")


if __name__ == "__main__":
    unittest.main()
