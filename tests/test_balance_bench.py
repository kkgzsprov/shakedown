"""The planted target and its fixed, disjoint evaluation plan stay explicit."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests
from shakedown.balance_bench import bench_balance
from shakedown.config import load_config


class BalanceBenchTests(unittest.TestCase):
    def test_fixed_search_holdout_and_primary_policy_qualification(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "qualification"
            gate = {"status": "passed", "targets": {"candidate": [
                {"policy": "rule", "distance": 0}, {"policy": "mock", "distance": 41}]}}
            def completed(config, campaign, approval, run_dir, **kwargs):
                self.assertTrue(kwargs["approved"])
                self.assertFalse(set(config.seeds) & set(config.raw["balance"]["holdout_seeds"]))
                self.assertEqual(len(config.raw["balance"]["holdout_seeds"]), 20)
                document = json.loads(Path(campaign).read_text(encoding="utf-8"))
                self.assertEqual(document["rounds"], 2)
                self.assertEqual(document["search_ranges"], {"player.speed": {"min": 1., "max": 5.}})
                self.assertEqual(json.loads(kwargs["targets"].read_text(encoding="utf-8"))["targets"][0]["min"], 55)
                return {"status": "completed", "gates": [deepcopy(gate)], "attempted_runs": 116,
                        "rounds": [{}, {}], "proposals": [{"path": "proposals/p001"}]}
            with patch("shakedown.balance_bench.run_balance_campaign", side_effect=completed):
                result = bench_balance(output)
            self.assertTrue(result["gate_passed"])
            self.assertEqual(result["runs"], 116)
            self.assertEqual(load_config(output / "shakedown.toml").raw["balance"]["policies"], ["rule", "mock"])
            self.assertEqual(json.loads((output / "benchmark.json").read_text())["gate_passed"], True)

    def test_missing_primary_measurement_never_passes_vacuously(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = {"status": "completed", "gates": [{"status": "passed", "targets": {"candidate": []}}],
                       "attempted_runs": 0, "rounds": [], "proposals": []}
            with patch("shakedown.balance_bench.run_balance_campaign", return_value=fixture):
                result = bench_balance(Path(temporary) / "empty")
            self.assertFalse(result["gate_passed"])


if __name__ == "__main__":
    unittest.main()
