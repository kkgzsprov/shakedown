"""Benchmark accounting and bounded real pipeline checks, not a second full run."""

from __future__ import annotations

from dataclasses import replace
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401
from shakedown import bench


ROOT = Path(__file__).resolve().parents[1]


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "benchmark"

    def test_frozen_seed_splits_are_disjoint_and_plan_ignores_only_fault_injection(self):
        manifest, _ = bench._seed_manifest(ROOT)
        self.assertFalse(set(manifest["search"]) & set(manifest["holdout"]))
        self.assertEqual(manifest["holdout"], [1009, 2003, 3001, 4001, 5003, 6007])
        clean = [{"id": "r1", "extra_args": ["--probe", "reload_fire"], "seed": 1009}]
        fault = [{"id": "r1", "extra_args": ["--probe", "reload_fire", "--bug", "ammo_underflow"], "seed": 1009}]
        self.assertEqual(bench._plan_hash(clean), bench._plan_hash(fault, "ammo_underflow"))
        fault[0]["seed"] = 999
        self.assertNotEqual(bench._plan_hash(clean), bench._plan_hash(fault, "ammo_underflow"))

    def _mock_pipeline(self, *, clean_findings=0, reproduced=True, omit_run=False):
        def explore(config, folder, *, max_runs, seed):
            arguments = config.raw["explore"]["variants"]
            rows = []
            for index, (run_seed, backend, variant) in enumerate(itertools.product(
                    config.seeds, config.backends, arguments), 1):
                rows.append({"id": f"r{index:06d}", "seed": run_seed, "map": "yard",
                             "profile": "cautious", "decider": backend, "variant": variant["name"],
                             "extra_args": variant["args"], "valid": True, "result": {}})
            rows = rows[:max_runs]
            if omit_run and folder.name == "clean":
                rows = rows[:-1]
            return {"status": "completed", "total_runs": len(rows), "invalid_runs": 0,
                    "findings": 0, "runs": rows, "plan": rows, "skipped_runs": []}

        def triage(config, batch_dir, output_dir, *, clips):
            bug = next((row["args"][-1] for row in config.raw["explore"]["variants"]
                        if "--bug" in row["args"]), None)
            if not bug:
                return {"entries": [{"id": "clean-false-finding"}] * clean_findings}
            mechanism, kind = bench.PRIMARY_CASES[bug]
            return {"entries": [{"id": "signature", "status": "reproduced" if reproduced else "unreproduced",
                                  "signature": {"mechanism": mechanism,
                                                "oracle" if mechanism == "oracle" else "signal": kind}}]}

        contexts = [patch.object(bench, "explore", side_effect=explore),
                    patch.object(bench, "analyze_batch", return_value={"findings": []}),
                    patch.object(bench, "triage_batch", side_effect=triage),
                    patch.object(bench, "_first_detectable", return_value=5),
                    patch.object(bench, "_visibility_diagnostic", return_value={"case": "vision_leak", "status": "passed"}),
                    patch.object(bench, "_clock_diagnostic", return_value={"case": "wallclock_load", "status": "passed"})]
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)

    def test_full_holdout_gate_requires_clean_controls_and_reproduced_seven_cases(self):
        self._mock_pipeline()
        result = bench.bench_tinyraid(self.output)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["qualified"])
        self.assertEqual(result["recall"], {"found": 7, "total": 7, "rate": 1.0, "full_primary_case_count": 7})
        self.assertEqual(result["precision"]["rate"], 1.0)
        self.assertTrue(all(row["plan_equal"] for row in result["cases"]))

    def test_clean_false_findings_fail_even_if_all_faults_are_reproduced(self):
        self._mock_pipeline(clean_findings=1)
        result = bench.bench_tinyraid(self.output)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["gate_passed"])
        self.assertLess(result["precision"]["rate"], 1)

    def test_matching_but_unreproduced_findings_do_not_count_as_recall(self):
        self._mock_pipeline(reproduced=False)
        result = bench.bench_tinyraid(self.output)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(row["found"] for row in result["cases"]))
        self.assertEqual(result["recall"]["found"], 0)

    def test_missing_actual_heldout_run_cannot_qualify_from_declared_budget(self):
        self._mock_pipeline(omit_run=True)
        result = bench.bench_tinyraid(self.output)
        self.assertEqual(result["status"], "unqualified")
        self.assertFalse(result["clean"]["complete_coverage"])

    def test_search_or_partial_runs_are_never_holdout_completion(self):
        self._mock_pipeline()
        search = bench.bench_tinyraid(self.output, split="search")
        self.assertEqual(search["status"], "unqualified")
        partial = bench.bench_tinyraid(self.output.parent / "partial", max_runs=1,
                                       cases=["ammo_underflow"], backends=["rule"])
        self.assertEqual(partial["status"], "unqualified")
        self.assertEqual(partial["recall"]["total"], 1)

    def test_omitting_diagnostics_is_an_unqualified_partial_benchmark(self):
        self._mock_pipeline()
        result = bench.bench_tinyraid(self.output, cases=list(bench.PRIMARY_CASES))
        self.assertEqual(result["recall"]["found"], 7)
        self.assertEqual(result["status"], "unqualified")
        self.assertFalse(result["gate_passed"])
        self.assertTrue(all(row["status"] == "unmeasured" for row in result["diagnostics"]))

    def test_invalid_arguments_and_existing_output_do_not_overwrite(self):
        for settings in ({"split": "training"}, {"max_runs": True}, {"backends": ["jev"]},
                         {"cases": ["unknown"]}, {"cases": ["thin_wall", "thin_wall"]}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                bench.bench_tinyraid(self.output, **settings)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("evidence")
        with self.assertRaises(FileExistsError):
            bench.bench_tinyraid(self.output)
        self.assertEqual(marker.read_text(), "evidence")

    def test_real_single_fault_pipeline_finds_and_independently_replays_numeric_failure(self):
        result = bench.bench_tinyraid(self.output, split="search", max_runs=7,
                                     backends=["rule"], cases=["ammo_underflow"])
        self.assertEqual(result["status"], "unqualified")
        self.assertEqual(result["clean"]["findings"], 0)
        self.assertTrue(result["cases"][0]["found"])
        self.assertTrue(result["cases"][0]["reproduced"])
        self.assertTrue(result["cases"][0]["plan_equal"])

    def test_controlled_clock_diagnostic_uses_real_stdio_and_repeats_the_mismatch(self):
        config = bench._configuration(ROOT, [1009], ("mock",), None)
        result = bench._clock_diagnostic(config, self.output, 1009)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["controlled_host_clock"])
        self.assertEqual([row["equal_telemetry"] for row in result["runs"]], [True, False, False])

    def test_interrupted_clean_exploration_stops_before_analysis_or_other_conditions(self):
        self._mock_pipeline()
        original = bench.explore.side_effect

        def interrupted(*args, **kwargs):
            result = original(*args, **kwargs)
            result["status"] = "interrupted"
            return result

        bench.explore.side_effect = interrupted
        result = bench.bench_tinyraid(self.output)
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse(result["qualified"])
        self.assertFalse(result["gate_passed"])
        self.assertEqual(bench.explore.call_count, 1)
        bench.analyze_batch.assert_not_called()
        bench.triage_batch.assert_not_called()
        bench._visibility_diagnostic.assert_not_called()
        self.assertEqual(result["clean"]["status"], "interrupted")
        self.assertTrue((self.output / "benchmark.md").is_file())

    def test_interrupted_fault_preserves_completed_case_and_does_not_launch_the_next(self):
        self._mock_pipeline()
        original = bench.explore.side_effect

        def interrupted(config, folder, **kwargs):
            result = original(config, folder, **kwargs)
            if folder.name == "thin_wall":
                result["status"] = "interrupted"
            return result

        bench.explore.side_effect = interrupted
        result = bench.bench_tinyraid(self.output)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual([row["case"] for row in result["cases"]], ["corner_pocket"])
        self.assertTrue(result["cases"][0]["reproduced"])
        self.assertEqual(result["interrupted_condition"]["case"], "thin_wall")
        self.assertEqual(bench.explore.call_count, 3)
        self.assertEqual(bench.analyze_batch.call_count, 2)
        self.assertEqual(bench.triage_batch.call_count, 2)
        bench._visibility_diagnostic.assert_not_called()

    def test_keyboard_interrupt_during_triage_or_diagnostic_persists_a_partial_report(self):
        self._mock_pipeline()
        original = bench.triage_batch.side_effect

        def cancelled_triage(config, batch_dir, output_dir, **kwargs):
            if batch_dir.name == "corner_pocket":
                raise KeyboardInterrupt
            return original(config, batch_dir, output_dir, **kwargs)

        bench.triage_batch.side_effect = cancelled_triage
        result = bench.bench_tinyraid(self.output)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["active_stage"], "triage")
        self.assertEqual(bench.explore.call_count, 2)
        self.assertIsNone(result["precision"]["rate"])
        bench._visibility_diagnostic.assert_not_called()
        bench.triage_batch.side_effect = original
        bench._visibility_diagnostic.side_effect = KeyboardInterrupt
        result = bench.bench_tinyraid(self.output.parent / "diagnostic-cancel")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(len(result["cases"]), 7)
        self.assertEqual(result["active_stage"], "diagnostic")
        bench._clock_diagnostic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
