"""Controlled workflows use the real tick input path and preserve replay."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import tests  # noqa: F401
from examples.tinyraid.game import Game
from examples.tinyraid.probes import PROBES, input_for_probe
from shakedown.bridge import run_bridge
from shakedown.deciders import make_decider


ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "navigate_corner": ("corner_pocket", "stuck", 14),
    "sprint_wall": ("thin_wall", "solid_collision", 3),
    "reload_fire": ("ammo_underflow", "numeric", 3),
    "heal_top_up": ("heal_overflow", "numeric", 3),
    "unlock_door": ("door_no_key", "door_without_key", 3),
    "grenade_escape": ("silent_grenade", None, 4),
    "crouched_extract": ("extract_crouch", None, 3),
}


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TinyraidProbeTests(unittest.TestCase):
    def execute(self, *arguments):
        return subprocess.run([sys.executable, "-m", "examples.tinyraid", *map(str, arguments)],
                              cwd=ROOT, capture_output=True, text=True, timeout=15)

    def test_probe_inputs_cannot_consult_the_enabled_fault(self):
        class TickOnly:
            def __init__(self, step):
                self.step = step

            def __getattr__(self, name):
                raise AssertionError(f"probe read hidden game field {name}")

        supplied = {"move": [0.0, 1.0], "stance": "stand"}
        for name in PROBES:
            for step in (0, 1, 29, 30, 600):
                with self.subTest(probe=name, step=step):
                    value = input_for_probe(name, TickOnly(step), supplied)
                    self.assertIsInstance(value, dict)
                    self.assertEqual(supplied, {"move": [0.0, 1.0], "stance": "stand"})
        with self.assertRaises(ValueError):
            input_for_probe("missing", TickOnly(0), {})

    def test_every_workflow_clean_control_and_single_fault_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for probe, (bug, oracle, duration) in CASES.items():
                for fault in (None, bug):
                    with self.subTest(probe=probe, fault=fault):
                        folder = base / probe / (fault or "clean")
                        telemetry, recording = folder / "telemetry.jsonl", folder / "inputs.json"
                        arguments = ["--probe", probe, "--seed", 101, "--duration", duration,
                                     "--telemetry", telemetry, "--record-inputs", recording]
                        if fault:
                            arguments.extend(("--bug", fault))
                        result = self.execute(*arguments)
                        expected_code = 4 if fault and oracle else 0
                        self.assertEqual(result.returncode, expected_code, result.stderr)
                        events = rows(telemetry)
                        end = events[-1]
                        if fault and oracle:
                            self.assertEqual(end["oracle"], oracle)
                        else:
                            self.assertIsNone(end["oracle"])
                        if probe == "grenade_escape":
                            self.assertEqual(end["outcome"], "death" if fault else "timeout")
                            self.assertEqual(end["counts"]["explosions"], 1)
                            self.assertEqual(end["counts"]["unwarned_explosion_deaths"], int(bool(fault)))
                        if probe == "crouched_extract":
                            self.assertEqual(end["outcome"], "timeout" if fault else "extract")
                            self.assertGreaterEqual(end["extract_zone_seconds"], 1)
                            self.assertGreater(end["extraction_attempts"], 0)
                            self.assertTrue(end["in_extraction_zone"])
                        document = json.loads(recording.read_text())
                        self.assertEqual(document["config"]["probe"], probe)
                        self.assertEqual(document["config"]["scenario"], PROBES[probe]["scenario"])
                        replay_file = folder / "replayed.jsonl"
                        replay = self.execute("--replay-inputs", recording, "--telemetry", replay_file)
                        self.assertEqual(replay.returncode, expected_code, replay.stderr)
                        self.assertEqual([row for row in events if row["type"] != "decision"], rows(replay_file))

    def test_original_seven_field_recordings_still_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            recording, telemetry = base / "inputs.json", base / "original.jsonl"
            result = self.execute("--duration", .5, "--record-inputs", recording, "--telemetry", telemetry)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(recording.read_text())
            self.assertNotIn("probe", document["config"])
            self.assertEqual(len(document["config"]), 7)
            replay = self.execute("--replay-inputs", recording)
            self.assertEqual(replay.returncode, 0, replay.stderr)

    def test_grenade_workflow_holds_walking_and_preserves_cautious_reflex_for_each_backend(self):
        self.assertEqual(input_for_probe("grenade_escape", object(),
                                        {"move": [1, 0], "fire": True}), {"stance": "stand"})
        self.assertEqual(input_for_probe("grenade_escape", object(),
                                        {"move": [1, 0], "sprint": True, "fire": True}),
                         {"stance": "stand", "move": [1, 0], "sprint": True})
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for backend in ("mock", "random"):
                for bug in (None, "silent_grenade"):
                    for seed in (101, 211, 307):
                        with self.subTest(backend=backend, bug=bug, seed=seed):
                            folder = base / f"{backend}-{bug}-{seed}"
                            command = [sys.executable, "-m", "examples.tinyraid", "--probe",
                                       "grenade_escape", "--duration", "4", "--seed", str(seed),
                                       "--decider", "bridge", "--telemetry", str(folder / "telemetry.jsonl")]
                            if bug:
                                command.extend(("--bug", bug))
                            code = run_bridge(command, folder, make_decider(backend, seed=seed),
                                              decider_name=backend, wall_timeout_s=10, cwd=ROOT)
                            self.assertEqual(code, 0)
                            end = rows(folder / "telemetry.jsonl")[-1]
                            self.assertEqual(end["outcome"], "death" if bug else "timeout")
                            self.assertEqual(end["counts"]["unwarned_explosion_deaths"], int(bool(bug)))

    def test_probe_recording_rejects_unknown_or_mismatched_scenario(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inputs.json"
            result = self.execute("--probe", "reload_fire", "--duration", .5, "--record-inputs", path)
            self.assertEqual(result.returncode, 0, result.stderr)
            original = json.loads(path.read_text())
            for probe, scenario in (("missing", "ammo_underflow"), ("reload_fire", "standard")):
                document = json.loads(json.dumps(original))
                document["config"].update(probe=probe, scenario=scenario)
                path.write_text(json.dumps(document), encoding="utf-8")
                replay = self.execute("--replay-inputs", path)
                self.assertEqual(replay.returncode, 2, replay.stderr)

    def test_extraction_dwell_measures_presence_not_only_button_requests(self):
        game = Game(scenario="extraction", duration=.1)
        for _ in range(6):
            game.tick({})
        end = game.events[-1]
        self.assertEqual(end["extract_zone_seconds"], .1)
        self.assertEqual(end["extraction_attempts"], 0)
        self.assertEqual(end["counts"]["explosions"], 0)
        self.assertEqual(end["counts"]["unwarned_explosion_deaths"], 0)

    def test_loot_opportunity_counts_each_crate_once_even_when_looted(self):
        game = Game(scenario="ammo_underflow", duration=.1)
        game.player.x, game.player.y = 5.5, 4.5
        for _ in range(6):
            game.tick({"loot": True})
        self.assertEqual(game.events[-1]["counts"]["loot_opportunities"], 1)
        self.assertEqual(game.events[-1]["counts"]["loot"], 10)

    def test_timeout_after_leaving_extraction_is_not_final_zone_occupancy(self):
        game = Game(scenario="extraction", duration=2)
        game.tick({"extract": True})
        for _ in range(30):
            game.tick({"move": [-1.0, 0.0]})
        while game.outcome is None:
            game.tick({})
        end = game.events[-1]
        self.assertEqual(end["outcome"], "timeout")
        self.assertGreater(end["extraction_attempts"], 0)
        self.assertGreater(end["extract_zone_seconds"], 0)
        self.assertIs(end["in_extraction_zone"], False)

    def test_search_and_reporting_seed_sets_are_frozen_and_disjoint(self):
        manifest = json.loads((ROOT / "examples/tinyraid/m3-seeds.json").read_text())
        self.assertEqual(manifest["search"], [101, 211, 307])
        self.assertEqual(manifest["holdout"], [1009, 2003, 3001, 4001, 5003, 6007])
        self.assertFalse(set(manifest["search"]) & set(manifest["holdout"]))


if __name__ == "__main__":
    unittest.main()
