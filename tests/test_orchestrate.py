"""M2 batch behavior at configuration, process, and result boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import tests  # noqa: F401 - Source checkout imports without an installation.

from shakedown.config import load_config
from shakedown.orchestrate import run_batch, run_case


ROOT = Path(__file__).resolve().parents[1]


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="shakedown batch ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "game project"
        self.project.mkdir()
        (self.project / "profile.json").write_text('{"counter":0}', encoding="utf-8")
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + self.env.get("PYTHONPATH", "")

    def config(self, *, mode: str = "normal", seeds=(1, 2), maps=("a",),
               profiles=("cautious",), backends=("rule",), timeout=5, label="fixture"):
        command = ["{python}", str(ROOT / "tests/batch_game.py"), "--seed", "{seed}",
                   "--map", "{map}", "--profile", "{profile}", "--seconds", "{seconds}",
                   "--telemetry", "{run_dir}/telemetry.jsonl", "--record-inputs",
                   "{run_dir}/inputs.json", "--decider", "{decider_mode}", "--tuning",
                   "{tuning_file}", "--mode", mode, "--label", label]
        path = self.project / "shakedown.toml"
        path.write_text(
            "[game]\n" + f"command = {json.dumps(command)}\n"
            + f"maps = {json.dumps(list(maps))}\nprofiles = {json.dumps(list(profiles))}\n"
            + 'fixtures = ["profile.json"]\n'
            + "[batch]\n" + f"seeds = {json.dumps(list(seeds))}\n"
            + f"deciders = {json.dumps(list(backends))}\nseconds = 1\nworkers = 2\n"
            + f"wall_timeout_s = {timeout}\nbootstrap_samples = 100\n",
            encoding="utf-8")
        return load_config(path)

    def rows(self, output: Path) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((output / "runs").glob("*/run.json"))]

    def test_parallel_runs_get_private_working_directories_and_fresh_fixtures(self) -> None:
        config = self.config(seeds=(1, 2, 3, 4))
        output = self.base / "parallel"
        result = run_batch(config, output, workers=4)
        self.assertEqual(result["total_runs"], 4)
        self.assertEqual(result["valid_runs"], 4)
        evidence = [json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted((output / "runs").glob("*/work/cwd-evidence.json"))]
        self.assertEqual(len(evidence), 4)
        self.assertEqual(len({item["cwd"] for item in evidence}), 4)
        self.assertTrue(all(item["initial"] == {"counter": 0} for item in evidence))
        self.assertEqual(json.loads((self.project / "profile.json").read_text()), {"counter": 0})

    def test_serial_and_parallel_batches_expand_the_same_specs_and_game_events(self) -> None:
        config = self.config(seeds=(7, 1), maps=("a", "b"), profiles=("cautious", "looter"),
                             backends=("rule", "mock"))
        first, second = self.base / "serial", self.base / "parallel"
        run_batch(config, first, workers=1)
        run_batch(config, second, workers=4)
        first_rows, second_rows = self.rows(first), self.rows(second)
        self.assertEqual(len(first_rows), 16)
        for left, right in zip(first_rows, second_rows):
            for key in ("id", "seed", "map", "profile", "decider", "tuning_hash", "valid"):
                self.assertEqual(left[key], right[key], key)
            self.assertEqual((first / "runs" / left["id"] / "telemetry.jsonl").read_bytes(),
                             (second / "runs" / right["id"] / "telemetry.jsonl").read_bytes())

    def test_completed_run_keeps_expanded_command_and_provenance(self) -> None:
        config = self.config(seeds=(7,))
        output = self.base / "provenance"
        result = run_case(config, output, seed=7, map_name="a", profile="cautious", decider="rule")
        stored = json.loads((output / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, result)
        self.assertEqual(stored["seed"], 7)
        self.assertEqual(stored["exit_code"], 0)
        self.assertTrue(stored["valid"])
        self.assertTrue(Path(stored["cwd"]).is_absolute())
        self.assertIn("7", stored["command"])
        self.assertFalse(any("{run_dir}" in item for item in stored["command"]))
        self.assertEqual(len(stored["tuning_hash"]), 64)
        self.assertIn("ordinary fixture output", (output / "stdout.log").read_text())

    def test_existing_output_directory_is_refused_without_overwriting_evidence(self) -> None:
        config = self.config()
        output = self.base / "existing"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_text("retained", encoding="utf-8")
        with self.assertRaises((ValueError, FileExistsError)):
            run_batch(config, output)
        self.assertEqual(marker.read_text(), "retained")
        self.assertEqual(list(output.iterdir()), [marker])

    def test_failed_runs_remain_visible_and_are_excluded_from_normal_metrics(self) -> None:
        config = self.config(mode="mixed", seeds=tuple(range(1, 8)))
        output = self.base / "mixed"
        manifest = run_batch(config, output)
        rows = self.rows(output)
        self.assertEqual(manifest["total_runs"], 7)
        self.assertEqual(len(rows), 7)
        by_seed = {row["seed"]: row for row in rows}
        self.assertTrue(by_seed[1]["valid"])
        self.assertTrue(by_seed[3]["valid"])
        self.assertEqual(by_seed[3]["status"], "finding")
        for seed in (2, 4, 5, 6, 7):
            with self.subTest(seed=seed):
                self.assertFalse(by_seed[seed]["valid"])
                self.assertTrue(by_seed[seed]["invalid_reasons"])
        metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["total_runs"], 7)
        self.assertEqual(metrics["valid_runs"], 2)
        self.assertEqual(len(metrics["invalid_runs"]), 5)
        self.assertTrue((output / "findings.jsonl").is_file())
        self.assertTrue((output / "batch.json").is_file())

    def test_missing_executable_records_every_failed_run(self) -> None:
        config = self.config(seeds=(1, 2))
        from dataclasses import replace
        config = replace(config, command=[str(self.project / "missing executable")])
        output = self.base / "missing-executable"
        manifest = run_batch(config, output)
        self.assertEqual(manifest["total_runs"], 2)
        self.assertEqual(len(self.rows(output)), 2)
        self.assertTrue(all(not row["valid"] and row["invalid_reasons"] for row in self.rows(output)))
        self.assertTrue((output / "metrics.json").is_file())

    def test_remapped_normal_and_oracle_exit_codes_remain_valid_metrics(self) -> None:
        from dataclasses import replace

        config = self.config(mode="mixed", seeds=(1, 3))
        config = replace(config, command=[*config.command, "--ok-code", "10", "--oracle-code", "41"],
                         exit_codes={"ok": 10, "contract": 2, "oracle": 41, "divergence": 5})
        output = self.base / "remapped"
        manifest = run_batch(config, output)
        self.assertEqual(manifest["valid_runs"], 2)
        self.assertEqual({row["exit_code"] for row in self.rows(output)}, {10, 41})
        self.assertEqual({row["status"] for row in self.rows(output)}, {"completed", "finding"})
        metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["valid_runs"], 2)
        self.assertEqual(metrics["invalid_runs"], [])

    def test_wall_clock_timeout_is_bounded_and_invalid(self) -> None:
        config = self.config(mode="timeout", seeds=(1,), timeout=.2)
        output = self.base / "timeout"
        started = time.monotonic()
        run_batch(config, output)
        elapsed = time.monotonic() - started
        row = self.rows(output)[0]
        self.assertLess(elapsed, 10, "timeout left a sleeping game blocking the batch")
        self.assertTrue(row["wall_timeout"])
        self.assertFalse(row["valid"])

    def test_shell_metacharacters_are_literal_game_arguments(self) -> None:
        marker = self.base / "shell-should-not-run.txt"
        label = f"a b; $(echo injected) & echo injected > {marker}"
        config = self.config(seeds=(1,), label=label)
        output = self.base / "literal"
        run_batch(config, output)
        evidence = json.loads(next((output / "runs").glob("*/work/cwd-evidence.json")).read_text())
        self.assertEqual(evidence["label"], label)
        self.assertFalse(marker.exists())

    def test_invalid_config_cli_exits_two_without_creating_output(self) -> None:
        path = self.project / "broken.toml"
        path.write_text("[game]\ncommand = [\"{unknown_placeholder}\"]\n", encoding="utf-8")
        output = self.base / "should-not-exist"
        result = subprocess.run([sys.executable, "-m", "shakedown", "run", "--config", str(path),
                                 "--output", str(output)], cwd=ROOT, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(output.exists())

    def test_cli_batch_overrides_expand_requested_seeds_and_deciders(self) -> None:
        config = self.config(seeds=(99,))
        output = self.base / "cli-overrides"
        result = subprocess.run([sys.executable, "-m", "shakedown", "run", "--config", str(config.path),
                                 "--output", str(output), "--workers", "2", "--seeds", "1,2",
                                 "--deciders", "rule,mock", "--seconds", "1"],
                                cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({(row["seed"], row["decider"]) for row in self.rows(output)},
                         {(1, "rule"), (1, "mock"), (2, "rule"), (2, "mock")})

    def test_sixty_real_tinyraid_runs_write_intervals_and_complete_manifests(self) -> None:
        config = load_config(ROOT / "examples/tinyraid/shakedown.toml")
        output = self.base / "tinyraid-sixty"
        result = run_batch(config, output, workers=4, duration=1)
        self.assertEqual(result["total_runs"], 60)
        self.assertEqual(result["valid_runs"], 60)
        self.assertEqual(len(self.rows(output)), 60)
        metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["valid_runs"], 60)
        self.assertEqual(len(metrics["groups"]), 6)
        for group in metrics["groups"]:
            self.assertEqual(group["runs"], 10)
            observed = [metric for metric in group["metrics"].values() if metric["n"] > 0]
            self.assertTrue(observed)
            self.assertTrue(all(metric["ci95"] is not None for metric in observed))

    def test_interruption_stops_active_work_and_retains_every_planned_attempt(self) -> None:
        from concurrent.futures import as_completed as actual_as_completed
        from dataclasses import replace

        config = self.config(seeds=tuple(range(1, 9)), timeout=5)
        output = self.base / "interrupted"
        actual_run_case = run_case

        def first_normal_then_sleep(project, run_dir, **kwargs):
            if kwargs["seed"] != 1:
                command = list(project.command)
                command[command.index("--mode") + 1] = "timeout"
                project = replace(project, command=command)
            return actual_run_case(project, run_dir, **kwargs)

        def interrupt_after_completed_attempt(futures):
            yield next(actual_as_completed(futures))
            # Wait for evidence that the second subprocess is active. This is
            # a condition wait, not a timing assumption about process startup.
            evidence = output / "runs/r000002/work/cwd-evidence.json"
            deadline = time.monotonic() + 2
            while not evidence.exists() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(evidence.exists(), "the cancellation probe never launched its game")
            initial = json.loads((output / "batch.json").read_text())
            self.assertEqual(len(initial["planned_runs"]), 8)
            self.assertEqual(initial["total_runs"], 8)
            raise KeyboardInterrupt

        started = time.monotonic()
        with mock.patch("shakedown.orchestrate.run_case", side_effect=first_normal_then_sleep), \
                mock.patch("shakedown.orchestrate.as_completed", side_effect=interrupt_after_completed_attempt):
            result = run_batch(config, output, workers=1)
        self.assertLess(time.monotonic() - started, 4.5, "interruption waited for the game timeout")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["total_runs"], 8)
        self.assertEqual(result["valid_runs"], 1)
        rows = self.rows(output)
        self.assertEqual(len(rows), 8)
        self.assertTrue(rows[0]["valid"], "the completed result was overwritten by cancellation")
        for row in rows[1:]:
            self.assertFalse(row["valid"])
            self.assertIn("cancelled", row["invalid_reasons"])
        self.assertEqual(len(list((output / "runs").glob("*/work/cwd-evidence.json"))), 2,
                         "queued games started after cancellation")
        metrics = json.loads((output / "metrics.json").read_text())
        self.assertEqual(metrics["total_runs"], 8)
        self.assertEqual(metrics["valid_runs"], 1)
        self.assertEqual(len(metrics["invalid_runs"]), 7)
        self.assertEqual(len((output / "findings.jsonl").read_text().splitlines()), 7)

    def test_unexpected_worker_exception_does_not_drop_planned_runs(self) -> None:
        config = self.config(seeds=(1, 2, 3))
        output = self.base / "worker-error"
        actual_run_case = run_case

        def failing_worker(project, run_dir, **kwargs):
            if kwargs["seed"] == 2:
                raise RuntimeError("injected worker failure before metadata")
            return actual_run_case(project, run_dir, **kwargs)

        with mock.patch("shakedown.orchestrate.run_case", side_effect=failing_worker):
            result = run_batch(config, output)
        self.assertEqual(result["total_runs"], 3)
        self.assertEqual(result["valid_runs"], 2)
        rows = self.rows(output)
        self.assertEqual(len(rows), 3)
        self.assertIn("worker_error", rows[1]["invalid_reasons"])
        self.assertIn("injected worker failure", rows[1]["error"])
        self.assertTrue(rows[0]["valid"] and rows[2]["valid"])


if __name__ == "__main__":
    unittest.main()
