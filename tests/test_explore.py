"""Coverage sampling and budget receipts, using actual isolated game runs."""

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tests  # noqa: F401
from shakedown.config import load_config
from shakedown.explore import explore
from shakedown.orchestrate import run_case


ROOT = Path(__file__).resolve().parents[1]


class ExploreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shakedown explore ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        project = self.base / "project"
        project.mkdir()
        argv = ["{python}", str(ROOT / "tests/batch_game.py"), "--seed", "{seed}",
                "--map", "{map}", "--profile", "{profile}", "--seconds", "{seconds}",
                "--telemetry", "{run_dir}/telemetry.jsonl", "--record-inputs",
                "{run_dir}/inputs.json", "--decider", "{decider_mode}", "--tuning", "{tuning_file}"]
        path = project / "shakedown.toml"
        path.write_text('[game]\ncommand = ' + json.dumps(argv)
                        + '\nmaps = ["a", "b"]\nprofiles = ["cautious"]\n'
                        + '[batch]\nseeds = [11, 29, 83]\ndeciders = ["rule", "mock"]\n'
                        + 'seconds = 1\nwall_timeout_s = 5\nbootstrap_samples = 50\n', encoding="utf-8")
        self.config = load_config(path)

    def configured(self, variants):
        raw = {**self.config.raw, "explore": {"variants": variants}}
        return replace(self.config, raw=raw)

    def test_seeded_steering_covers_every_cell_before_repeating(self):
        variants = [{"name": "day", "args": ["--label", "day"]},
                    {"name": "night", "args": ["--label", "night"]}]
        config = self.configured(variants)
        first = explore(config, self.base / "first", max_runs=16, seed=7)
        second = explore(config, self.base / "second", max_runs=16, seed=7)
        self.assertEqual(first["plan"], second["plan"])
        cells = [tuple(item[key] for key in ("map", "profile", "decider", "variant"))
                 for item in first["plan"]]
        self.assertEqual(len(set(cells[:8])), 8)
        self.assertEqual(set(Counter(cells).values()), {2})
        self.assertEqual(first["total_runs"], 16)
        self.assertEqual(first["valid_runs"], 16)
        self.assertEqual(first["stop_reason"], "run_budget")
        self.assertTrue(all(item["seed"] in config.seeds for item in first["plan"]))
        self.assertEqual([row["id"] for row in first["runs"]], [f"r{i:06d}" for i in range(1, 17)])
        for row in first["runs"]:
            stored = json.loads((self.base / "first/runs" / row["id"] / "run.json").read_text())
            self.assertEqual(stored["variant"], row["variant"])
            self.assertEqual(stored["extra_args"], ["--label", row["variant"]])
            evidence = json.loads((self.base / "first/runs" / row["id"] / "work/cwd-evidence.json").read_text())
            self.assertEqual(evidence["label"], row["variant"])

    def test_different_sampler_seed_changes_draws_without_changing_the_budget(self):
        first = explore(self.config, self.base / "first", max_runs=6, seed=1, steer=False)
        second = explore(self.config, self.base / "second", max_runs=6, seed=2, steer=False)
        self.assertNotEqual(first["plan"], second["plan"])
        self.assertEqual((first["total_runs"], second["total_runs"]), (6, 6))

    def test_each_cell_exhausts_distinct_seed_pool_before_reusing_a_seed(self):
        result = explore(self.config, self.base / "cycles", max_runs=24, seed=13)
        cells = {}
        for item in result["plan"]:
            key = (item["map"], item["profile"], item["decider"], item["variant"])
            cells.setdefault(key, []).append(item["seed"])
        self.assertEqual(len(cells), 4)
        for seeds in cells.values():
            self.assertEqual(len(seeds), 6)
            self.assertEqual(set(seeds[:3]), set(self.config.seeds))
            self.assertEqual(set(seeds[3:]), set(self.config.seeds))

    def test_tuning_noise_stays_inside_exact_key_bounds_and_preserves_unselected_values(self):
        keys = {"count": {"type": "int", "min": 2**53, "max": 2**53 + 3},
                "speed": {"type": "float", "min": .1, "max": .9},
                "damage": {"type": "float", "min": 1, "max": 2}}
        (self.config.path.parent / "keys.json").write_text(json.dumps({"keys": keys}))
        raw = {**self.config.raw, "tuning": {"keys": "keys.json"}}
        config = replace(self.config, raw=raw, tuning_candidates=({"fixed": 5},))
        result = explore(config, self.base / "noise", max_runs=8, seed=17, tuning_noise=3)
        for spec in result["plan"]:
            self.assertGreaterEqual(len(spec["noise_keys"]), 1)
            self.assertLessEqual(len(spec["noise_keys"]), 3)
            self.assertEqual(spec["tuning"]["fixed"], 5)
            for name in spec["noise_keys"]:
                value = spec["tuning"][name]
                self.assertGreaterEqual(value, keys[name]["min"])
                self.assertLessEqual(value, keys[name]["max"])
                if name == "count":
                    self.assertIs(type(value), int)
        inputs = json.loads((self.base / "noise/runs/r000001/tuning.json").read_text())
        self.assertEqual(inputs["values"], result["plan"][0]["tuning"])

    def test_zero_deadline_keeps_the_plan_but_starts_no_game(self):
        with mock.patch("shakedown.explore.run_case") as runner:
            result = explore(self.config, self.base / "deadline", max_runs=5, deadline_seconds=0)
        runner.assert_not_called()
        self.assertEqual(result["total_runs"], 0)
        self.assertEqual(len(result["plan"]), 5)
        self.assertEqual(len(result["skipped_runs"]), 5)
        self.assertEqual(result["stop_reason"], "deadline")
        self.assertTrue(all(item["reason"] == "not_started_deadline" for item in result["skipped_runs"]))
        metrics = json.loads((self.base / "deadline/metrics.json").read_text())
        self.assertEqual((metrics["total_runs"], metrics["valid_runs"], metrics["invalid_runs"]), (0, 0, []))

    def test_finite_float_noise_bounds_do_not_overflow_their_width(self):
        path = self.config.path.parent / "wide-keys.json"
        path.write_text(json.dumps({"keys": {"wide": {"type": "float", "min": -1e308, "max": 1e308}}}))
        config = replace(self.config, raw={**self.config.raw, "tuning": {"keys": path.name}})
        result = explore(config, self.base / "wide", max_runs=2, tuning_noise=1)
        for spec in result["plan"]:
            self.assertGreaterEqual(spec["tuning"]["wide"], -1e308)
            self.assertLessEqual(spec["tuning"]["wide"], 1e308)
        self.assertEqual(result["valid_runs"], 2)

    def test_deadline_consumed_by_checkpoint_does_not_submit_a_run(self):
        with mock.patch("shakedown.explore.time.monotonic", side_effect=[0, .2, 1.1]), \
                mock.patch("shakedown.explore.run_case") as runner:
            result = explore(self.config, self.base / "deadline", max_runs=3, deadline_seconds=1)
        runner.assert_not_called()
        self.assertEqual((result["total_runs"], len(result["skipped_runs"])), (0, 3))

    def test_running_game_uses_only_the_remaining_wall_budget(self):
        captured = []
        clock = iter((0, .1, .3, 1.1))

        def bounded(config, *args, **kwargs):
            captured.append(config.wall_timeout)
            return run_case(config, *args, **kwargs)

        with mock.patch("shakedown.explore.time.monotonic", side_effect=lambda: next(clock)), \
                mock.patch("shakedown.explore.run_case", side_effect=bounded):
            result = explore(self.config, self.base / "deadline", max_runs=3, deadline_seconds=1)
        self.assertAlmostEqual(captured[0], .7)
        self.assertEqual((result["total_runs"], len(result["skipped_runs"])), (1, 2))

    def test_interruption_retains_the_started_attempt_and_skips_unsubmitted_draws(self):
        def interrupted(config, output, **kwargs):
            output.mkdir()
            raise KeyboardInterrupt

        with mock.patch("shakedown.explore.run_case", side_effect=interrupted):
            result = explore(self.config, self.base / "interrupted", max_runs=4)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["stop_reason"], "interrupted")
        self.assertEqual((result["total_runs"], result["invalid_runs"], len(result["skipped_runs"])), (1, 1, 3))
        self.assertIn("cancelled", result["runs"][0]["invalid_reasons"])
        self.assertTrue((self.base / "interrupted/runs/r000001/run.json").is_file())

    def test_failed_launch_is_retained_and_exploration_continues_without_retry(self):
        config = replace(self.config, command=["missing-shakedown-explore-game"])
        result = explore(config, self.base / "failures", max_runs=3)
        self.assertEqual((result["total_runs"], result["valid_runs"], result["invalid_runs"]), (3, 0, 3))
        self.assertEqual(result["findings"], 3)
        self.assertEqual(len((self.base / "failures/findings.jsonl").read_text(encoding="utf-8").splitlines()), 3)

    def test_existing_output_is_never_overwritten(self):
        directory = self.base / "existing"
        directory.mkdir()
        marker = directory / "keep"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            explore(self.config, directory, max_runs=1)
        self.assertEqual(list(directory.iterdir()), [marker])

    def test_invalid_options_are_rejected_before_creating_output(self):
        cases = ({"max_runs": -1}, {"max_runs": True}, {"deadline_seconds": float("nan")},
                 {"deadline_seconds": -1}, {"deadline_seconds": 10**400}, {"seed": True},
                 {"tuning_noise": 4}, {"tuning_noise": True}, {"steer": 1}, {"tuning_noise": 1})
        for index, kwargs in enumerate(cases):
            with self.subTest(kwargs=kwargs):
                output = self.base / f"bad-{index}"
                with self.assertRaises(ValueError):
                    explore(self.config, output, **kwargs)
                self.assertFalse(output.exists())
        malformed = ({"variants": []}, {"unknown": 1}, {"variants": [{"name": "x", "args": "--label"}]},
                     {"variants": [{"name": "x", "args": []}, {"name": "x", "args": []}]})
        for index, settings in enumerate(malformed):
            with self.subTest(settings=settings):
                config = replace(self.config, raw={**self.config.raw, "explore": settings})
                with self.assertRaises(ValueError):
                    explore(config, self.base / f"bad-variant-{index}", max_runs=1)


if __name__ == "__main__":
    unittest.main()
