"""M1 acceptance checks against the real tinyraid process and input path."""

from __future__ import annotations

import itertools
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import tests  # noqa: F401 - Make src available without an installation.

from shakedown.protocol import PREFIX
from shakedown.bridge import run_bridge
from shakedown.deciders import make_decider


ROOT = Path(__file__).resolve().parents[1]


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def game_events(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["type"] != "decision"]


class TinyraidAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = os.pathsep.join(
            (str(ROOT), str(ROOT / "src"), self.env.get("PYTHONPATH", "")))

    def command(self, *args: object) -> list[str]:
        return [sys.executable, "-m", "examples.tinyraid", *map(str, args)]

    def execute(self, command: list[str], *, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(command, cwd=ROOT, env=self.env, input=stdin,
                              capture_output=True, text=True, timeout=30)

    def run_game(self, name: str, *args: object, duration: float = 12,
                 seed: int = 7, mode: str = "rule") -> tuple[subprocess.CompletedProcess, Path]:
        run_dir = self.base / name
        run_dir.mkdir()
        result = self.execute(self.command(
            "--headless", "--seed", seed, "--duration", duration,
            "--decider", mode, "--telemetry", run_dir / "telemetry.jsonl",
            "--record-inputs", run_dir / "inputs.json", *args))
        return result, run_dir

    def run_bridge(self, name: str, *, backend: str = "mock", duration: float = 12,
                   seed: int = 7) -> tuple[subprocess.CompletedProcess, Path]:
        run_dir = self.base / name
        run_dir.mkdir()
        result = self.execute([
            sys.executable, "-m", "shakedown", "bridge", "--decider", backend,
            "--seed", str(seed), "--wall-timeout", "20", "--run-dir", str(run_dir),
            "--", *self.command("--headless", "--seed", seed, "--duration", duration,
                                "--decider", "bridge", "--telemetry", run_dir / "telemetry.jsonl",
                                "--record-inputs", run_dir / "inputs.json")])
        return result, run_dir

    def replay(self, name: str, recording: Path) -> tuple[subprocess.CompletedProcess, Path]:
        run_dir = self.base / name
        run_dir.mkdir()
        telemetry = run_dir / "telemetry.jsonl"
        result = self.execute(self.command("--replay-inputs", recording, "--telemetry", telemetry))
        return result, telemetry

    def assert_success(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_c1_rule_runs_have_identical_telemetry_and_inputs(self) -> None:
        first_result, first = self.run_game("first")
        second_result, second = self.run_game("second")
        self.assert_success(first_result)
        self.assert_success(second_result)
        for filename in ("telemetry.jsonl", "inputs.json"):
            with self.subTest(filename=filename):
                self.assertEqual((first / filename).read_bytes(), (second / filename).read_bytes())

    def test_c2_recorded_rule_and_mock_inputs_replay_without_a_decider(self) -> None:
        for mode in ("rule", "mock"):
            with self.subTest(mode=mode):
                result, original = (self.run_game(mode) if mode == "rule"
                                    else self.run_bridge(mode))
                self.assert_success(result)
                replay_result, replay_telemetry = self.replay(
                    f"replay-{mode}", original / "inputs.json")
                self.assert_success(replay_result)
                self.assertEqual(game_events(records(original / "telemetry.jsonl")),
                                 game_events(records(replay_telemetry)))
                self.assertFalse(any(row["type"] == "decision" for row in records(replay_telemetry)))

    def test_c3_mock_bridge_tape_replays_in_the_adapter(self) -> None:
        original_result, original = self.run_bridge("original")
        self.assert_success(original_result)
        tape = original / "decision_tape.jsonl"
        self.assertGreater(len(records(tape)), 1)
        replay_result, replay = self.run_game("tape-replay", mode=f"tape:{tape}")
        self.assert_success(replay_result)
        first = [row for row in records(original / "telemetry.jsonl") if row["type"] != "run_begin"]
        second = [row for row in records(replay / "telemetry.jsonl") if row["type"] != "run_begin"]
        self.assertEqual(first, second)
        self.assertEqual(sum(row["type"] == "decision" and row["status"] == "selected"
                             for row in first), len(records(tape)))

    def test_c4_bad_answer_sequence_choice_json_and_eof_exit_two(self) -> None:
        answers = {
            "seq": '{"v":1,"seq":-999,"choice":"hold"}\n',
            "choice": '{"v":1,"seq":1,"choice":"not_a_candidate"}\n',
            "json": "{broken\n",
            "eof": "",
        }
        for fault, answer in answers.items():
            with self.subTest(fault=fault):
                result = self.execute(self.command("--decider", "bridge", "--duration", 1),
                                      stdin=answer)
                self.assertEqual(result.returncode, 2, result.stderr)
                messages = [json.loads(line[len(PREFIX):]) for line in result.stdout.splitlines()
                            if line.startswith(PREFIX)]
                self.assertEqual(messages[0]["type"], "hello")
                request = next(message for message in messages if message["type"] == "decide")
                self.assertEqual(request["seq"], 1)
                self.assertIn("hold", request["candidates"])

    def test_c5_tampered_exhausted_and_unconsumed_tapes_exit_five(self) -> None:
        original_result, original = self.run_bridge("original")
        self.assert_success(original_result)
        original_rows = records(original / "decision_tape.jsonl")
        self.assertGreater(len(original_rows), 1)
        for fault in ("hash", "choice", "exhausted", "unconsumed"):
            with self.subTest(fault=fault):
                rows = [dict(row) for row in original_rows]
                if fault == "hash":
                    rows[0]["hash"] = "0" * 64
                elif fault == "choice":
                    rows[0]["choice"] = "not_a_candidate"
                elif fault == "exhausted":
                    rows.pop()
                else:
                    rows.append(dict(rows[-1]))
                tape = self.base / f"{fault}.jsonl"
                tape.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                result, _ = self.run_game(fault, mode=f"tape:{tape}")
                self.assertEqual(result.returncode, 5, result.stderr)

    def test_c6_forced_stuck_exit_and_recording_reproduce_the_oracle(self) -> None:
        original_result, original = self.run_game("stuck", "--scenario", "stuck", duration=14)
        self.assertEqual(original_result.returncode, 4, original_result.stderr)
        first = records(original / "telemetry.jsonl")
        self.assertEqual(first[-1]["type"], "run_end")
        self.assertEqual(first[-1]["outcome"], "oracle")
        self.assertEqual(first[-1]["oracle"], "stuck")
        replay_result, replay_telemetry = self.replay("stuck-replay", original / "inputs.json")
        self.assertEqual(replay_result.returncode, 4, replay_result.stderr)
        self.assertEqual(game_events(first), game_events(records(replay_telemetry)))

    def test_corner_pocket_is_found_despite_normal_tactic_replanning_and_replays(self) -> None:
        original_result, original = self.run_game("corner-pocket", "--scenario", "corner_pocket",
                                                   "--bug", "corner_pocket", duration=25)
        self.assertEqual(original_result.returncode, 4, original_result.stderr)
        first = records(original / "telemetry.jsonl")
        self.assertEqual(first[-1]["oracle"], "stuck")
        self.assertGreater(sum(row["type"] == "decision" for row in first), 2)
        replay_result, replay_telemetry = self.replay("corner-replay", original / "inputs.json")
        self.assertEqual(replay_result.returncode, 4, replay_result.stderr)
        self.assertEqual(game_events(first), game_events(records(replay_telemetry)))

    def test_normal_adapter_survives_warned_grenade_and_silent_death_inputs_replay(self) -> None:
        for bug, expected in ((None, "timeout"), ("silent_grenade", "death")):
            with self.subTest(bug=bug):
                args = ["--scenario", "grenade"]
                if bug:
                    args += ["--bug", bug]
                name = bug or "warned"
                result, original = self.run_game(name, *args, duration=4)
                self.assert_success(result)
                first = records(original / "telemetry.jsonl")
                self.assertEqual(first[-1]["outcome"], expected)
                replay_result, replay_telemetry = self.replay(f"{name}-replay", original / "inputs.json")
                self.assert_success(replay_result)
                self.assertEqual(game_events(first), game_events(records(replay_telemetry)))
                if bug:
                    death = next(row for row in first if row["type"] == "death")
                    self.assertEqual(death["cause"], "explosion")
                    self.assertFalse(death["warned"])

    def test_clean_m1_seed_matrix_has_no_findings(self) -> None:
        manifest = json.loads((ROOT / "examples/tinyraid/m1-seeds.json").read_text(encoding="utf-8"))
        combinations = list(itertools.product(manifest["seeds"], manifest["maps"], manifest["profiles"]))
        self.assertEqual(len(combinations), 18)
        for seed, map_name, profile in combinations:
            with self.subTest(seed=seed, map=map_name, profile=profile):
                result, run_dir = self.run_game(f"{seed}-{map_name}-{profile}", "--map", map_name,
                                                "--profile-name", profile, seed=seed,
                                                duration=manifest["duration"])
                self.assert_success(result)
                rows = records(run_dir / "telemetry.jsonl")
                self.assertFalse(any(row["type"] == "oracle" for row in rows))
                self.assertIn(rows[-1]["outcome"], ("death", "extract", "timeout"))

    def test_random_bridge_inputs_replay(self) -> None:
        result, original = self.run_bridge("random", backend="random", duration=18)
        self.assert_success(result)
        replay_result, replay_telemetry = self.replay("random-replay", original / "inputs.json")
        self.assert_success(replay_result)
        self.assertEqual(game_events(records(original / "telemetry.jsonl")),
                         game_events(records(replay_telemetry)))

    def test_input_records_cover_exact_simulation_steps(self) -> None:
        result, original = self.run_game("timestamps", duration=1.25)
        self.assert_success(result)
        recording = json.loads((original / "inputs.json").read_text(encoding="utf-8"))
        inputs = recording["inputs"]
        self.assertEqual(len(inputs), 75)
        for step, item in enumerate(inputs):
            self.assertEqual(item["step"], step)
            self.assertEqual(item["t"], step / 60)
            self.assertIsInstance(item["input"], dict)

    def test_replay_rejects_missing_duplicate_and_shifted_input_steps(self) -> None:
        result, original = self.run_game("timestamps", duration=1)
        self.assert_success(result)
        original_recording = json.loads((original / "inputs.json").read_text(encoding="utf-8"))
        for fault in ("missing", "duplicate", "time"):
            with self.subTest(fault=fault):
                recording = json.loads(json.dumps(original_recording))
                if fault == "missing":
                    recording["inputs"].pop(1)
                elif fault == "duplicate":
                    recording["inputs"][1]["step"] = 0
                else:
                    recording["inputs"][1]["t"] += 1 / 60
                path = self.base / f"bad-{fault}.json"
                path.write_text(json.dumps(recording), encoding="utf-8")
                replay_result, _ = self.replay(f"replay-{fault}", path)
                self.assertEqual(replay_result.returncode, 2, replay_result.stderr)

    def test_decision_receipts_apply_after_the_selected_simulation_step(self) -> None:
        result, run_dir = self.run_game("delay", duration=5)
        self.assert_success(result)
        decisions = [row for row in records(run_dir / "telemetry.jsonl") if row["type"] == "decision"]
        selected = {row["seq"]: row for row in decisions if row["status"] == "selected"}
        applied = [row for row in decisions if row["status"] == "applied"]
        self.assertTrue(applied)
        for row in applied:
            self.assertGreaterEqual(row["applied_step"] - selected[row["seq"]]["step"], 18)
            self.assertEqual(row["step"], row["applied_step"])
        for row in decisions:
            self.assertNotIn("latency_ms", row)
            self.assertNotIn("source", row)

    def test_invalid_tuning_and_duration_are_contract_errors(self) -> None:
        for tuning in ({"unknown.key": 1}, {"player.speed": -1}):
            with self.subTest(tuning=tuning):
                path = self.base / "tuning.json"
                path.write_text(json.dumps({"tuning_version": 1, "values": tuning}), encoding="utf-8")
                result = self.execute(self.command("--tuning", path))
                self.assertEqual(result.returncode, 2, result.stderr)
        for duration in ("nan", "inf", "0", "-1"):
            with self.subTest(duration=duration):
                result = self.execute(self.command("--duration", duration))
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_c7_adapter_hides_guards_behind_walls_and_bug_fixture_exposes_them(self) -> None:
        for bug, expected_visible in ((None, False), ("vision_leak", True)):
            with self.subTest(bug=bug):
                args = ["--scenario", "vision", "--decider", "bridge", "--duration", "1"]
                if bug:
                    args += ["--bug", bug]
                result = self.execute(self.command(*args))
                self.assertEqual(result.returncode, 2, result.stderr)  # EOF after observing the request.
                messages = [json.loads(line[len(PREFIX):]) for line in result.stdout.splitlines()
                            if line.startswith(PREFIX)]
                request = next(message for message in messages if message["type"] == "decide")
                self.assertEqual(bool(request["state"]["seen"]), expected_visible)

    def test_valid_changed_input_is_reported_as_a_replay_assertion_failure(self) -> None:
        result, original = self.run_game("original", duration=1)
        self.assert_success(result)
        recording = json.loads((original / "inputs.json").read_text(encoding="utf-8"))
        recording["inputs"][0]["input"]["move"] = [0, 1]
        changed = self.base / "changed.json"
        changed.write_text(json.dumps(recording), encoding="utf-8")
        replay_result, _ = self.replay("changed-replay", changed)
        self.assertEqual(replay_result.returncode, 3, replay_result.stderr)
        self.assertIn("replay assertion", replay_result.stderr)

    def test_malformed_recording_metadata_and_extreme_numbers_exit_two(self) -> None:
        result, original = self.run_game("original", duration=1)
        self.assert_success(result)
        original_recording = json.loads((original / "inputs.json").read_text(encoding="utf-8"))
        for fault in ("boolean-version", "missing-config", "huge-duration", "huge-input", "invalid-profile"):
            with self.subTest(fault=fault):
                recording = json.loads(json.dumps(original_recording))
                if fault == "boolean-version":
                    recording["v"] = True
                elif fault == "missing-config":
                    del recording["config"]["seed"]
                elif fault == "huge-duration":
                    recording["config"]["duration"] = 10 ** 400
                elif fault == "huge-input":
                    recording["inputs"][0]["input"]["move"] = [10 ** 400, 0]
                else:
                    recording["config"]["profile"] = []
                path = self.base / f"bad-{fault}.json"
                path.write_text(json.dumps(recording), encoding="utf-8")
                replay_result, _ = self.replay(f"replay-{fault}", path)
                self.assertEqual(replay_result.returncode, 2, replay_result.stderr)
                self.assertNotIn("Traceback", replay_result.stderr)

    def test_slow_decider_pauses_clean_simulation_without_changing_tape_or_input_replay(self) -> None:
        class SlowFirstMock:
            def __init__(self):
                self.mock = make_decider("mock")
                self.first = True

            def decide(self, request, instructions):
                if self.first:
                    time.sleep(0.2)
                    self.first = False
                return self.mock.decide(request, instructions)

            def close(self):
                self.mock.close()

        original = self.base / "slow"
        original.mkdir()
        command = self.command("--duration", 2, "--seed", 7, "--decider", "bridge",
                               "--telemetry", original / "telemetry.jsonl",
                               "--record-inputs", original / "inputs.json")
        code = run_bridge(command, original, SlowFirstMock(), decider_name="mock", wall_timeout_s=10)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(records(original / "decision_tape.jsonl")[0]["latency_ms"], 180)
        tape_result, tape_replay = self.run_game(
            "slow-tape-replay", duration=2, mode=f"tape:{original / 'decision_tape.jsonl'}")
        self.assert_success(tape_result)
        self.assertEqual(records(original / "telemetry.jsonl"), records(tape_replay / "telemetry.jsonl"))
        input_result, input_telemetry = self.replay("slow-input-replay", original / "inputs.json")
        self.assert_success(input_result)
        self.assertEqual(game_events(records(original / "telemetry.jsonl")),
                         game_events(records(input_telemetry)))

    def test_wallclock_bug_breaks_tape_telemetry_under_a_controlled_host_clock(self) -> None:
        # Advance only the child's host clock when its answer arrives. The fast tape
        # never reads stdin, so no host time elapses. This tests the planted defect
        # without requiring CI to execute a simulation inside a wall-time deadline.
        script = """
import sys
from unittest.mock import patch
from examples.tinyraid.__main__ import main
clock = [0.0]
original_stdin = sys.stdin
class AnswerStream:
    @property
    def buffer(self):
        return self
    def readline(self):
        answer = original_stdin.buffer.readline()
        clock[0] = 0.2
        return answer
sys.stdin = AnswerStream()
with patch('time.monotonic', lambda: clock[0]):
    raise SystemExit(main())
"""
        original = self.base / "wallclock-original"
        original.mkdir()
        common = ["--duration", "2", "--seed", "7", "--bug", "wallclock_load"]
        code = run_bridge(
            [sys.executable, "-c", script, *common, "--decider", "bridge",
             "--telemetry", str(original / "telemetry.jsonl")],
            original, make_decider("mock"), decider_name="mock", wall_timeout_s=10)
        self.assertEqual(code, 0)
        telemetry = self.base / "wallclock-replay.jsonl"
        result = self.execute([sys.executable, "-c", script, *common, "--decider",
                               f"tape:{original / 'decision_tape.jsonl'}", "--telemetry", str(telemetry)])
        self.assert_success(result)
        first = records(original / "telemetry.jsonl")
        second = records(telemetry)
        self.assertTrue(any(row["type"] == "asset_loaded" for row in first))
        self.assertFalse(any(row["type"] == "asset_loaded" for row in second))
        self.assertNotEqual(game_events(first), game_events(second))


class TinyraidSimulationTests(unittest.TestCase):
    def test_same_tick_extraction_cannot_mask_a_numeric_oracle(self) -> None:
        from examples.tinyraid.game import Game

        for bug, expected in ((None, "extract"), ("heal_overflow", "oracle")):
            with self.subTest(bug=bug):
                game = Game(scenario="extraction", bug=bug, duration=5)
                for step in range(1, 122):
                    inputs = {"heal": True} if step == 1 else {}
                    if step >= 62:
                        inputs["extract"] = True
                    game.tick(inputs)
                self.assertEqual(game.outcome, expected)
                self.assertEqual(game.oracle, "numeric" if bug else None)
                self.assertEqual(game.events[-1]["outcome"], expected)
                if bug:
                    self.assertFalse(any(event["type"] == "extract" for event in game.events))

    def game(self, **kwargs):
        from examples.tinyraid.game import Game
        return Game(scenario="ammo_underflow", **kwargs)

    def test_one_second_movement_is_fixed_step_and_diagonals_are_normalized(self) -> None:
        straight = self.game()
        diagonal = self.game()
        origin = straight.position
        for _ in range(60):
            straight.tick({"move": [1, 0]})
            diagonal.tick({"move": [1, 1]})
        self.assertEqual(straight.time, 1)
        self.assertAlmostEqual(math.dist(origin, straight.position), 3)
        self.assertAlmostEqual(math.dist(origin, diagonal.position), 3)

    def test_pathfinding_routes_around_walls_through_the_same_movement_input(self) -> None:
        game = self.game()
        game.player.x, game.player.y = 2.5, 7.5
        target = (8.5, 10.5)
        self.assertFalse(game.line_of_sight(game.position, target))
        path = game.path_to(target)
        self.assertGreater(len(path), 1)
        previous = game.position
        for waypoint in path:
            self.assertTrue(game.line_of_sight(previous, waypoint))
            while math.dist(game.position, waypoint) > 0.05:
                self.assertIsNone(game.outcome)
                dx, dy = waypoint[0] - game.player.x, waypoint[1] - game.player.y
                length = math.hypot(dx, dy)
                game.tick({"move": [dx / length, dy / length]})
            previous = waypoint
        self.assertLess(math.dist(game.position, target), 0.06)
        self.assertIsNone(game.oracle)

    def test_clean_collision_blocks_sprinting_but_seeded_gap_trips_geometry_oracle(self) -> None:
        from examples.tinyraid.game import Game
        clean = Game(scenario="thin_wall")
        for _ in range(120):
            clean.tick({"move": [1, 0], "sprint": True})
        self.assertLess(clean.player.x, 11)
        self.assertIsNone(clean.oracle)
        buggy = Game(scenario="thin_wall", bug="thin_wall")
        for _ in range(120):
            buggy.tick({"move": [1, 0], "sprint": True})
            if buggy.outcome:
                break
        self.assertEqual(buggy.oracle, "solid_collision")

    def test_reload_prevents_clean_fire_and_conserves_total_ammo(self) -> None:
        game = self.game()
        initial = game.player.ammo + game.player.reserve
        game.tick({"fire": True, "reload": True})
        self.assertEqual(game.player.ammo, 1)
        for _ in range(60):
            game.tick({})
        self.assertEqual(game.player.ammo, 8)
        self.assertEqual(game.player.ammo + game.player.reserve, initial)
        self.assertFalse(any(row["type"] == "shot" for row in game.events))

    def test_aimed_fire_uses_ammo_and_cooldown_and_can_kill_a_guard(self) -> None:
        from examples.tinyraid.game import Guard
        game = self.game()
        game.player.ammo = 8
        guard = Guard("g1", 5.5, 2.5)
        game.guards = [guard]
        for _ in range(13):
            game.tick({"aim": [guard.x, guard.y], "fire": True})
        self.assertEqual(guard.hp, 0)
        self.assertEqual(game.player.ammo, 6)
        self.assertEqual(sum(row["type"] == "shot" for row in game.events), 2)
        self.assertEqual(sum(row["type"] == "guard_down" for row in game.events), 1)

    def test_healing_finishes_after_two_seconds_and_consumes_one_item(self) -> None:
        game = self.game()
        game.player.hp = 90
        game.tick({"heal": True})
        self.assertEqual(game.player.hp, 90)
        self.assertEqual(game.player.heals, 2)
        for _ in range(119):
            game.tick({})
        self.assertEqual(game.player.hp, 90)
        game.tick({})
        self.assertEqual(game.player.hp, 100)
        self.assertEqual(game.player.heals, 1)

    def test_seeded_numeric_bugs_have_distinct_reproducible_input_triggers(self) -> None:
        from examples.tinyraid.game import Game
        for bug, action, ticks in (("ammo_underflow", {"fire": True, "reload": True}, 1),
                                    ("heal_overflow", {"heal": True}, 121)):
            with self.subTest(bug=bug):
                game = Game(scenario=bug, bug=bug)
                game.tick(action)
                for _ in range(ticks - 1):
                    game.tick({})
                self.assertEqual(game.outcome, "oracle")
                self.assertEqual(game.oracle, "numeric")

    def test_door_rejects_no_key_and_bug_is_caught_by_its_oracle(self) -> None:
        from examples.tinyraid.game import Game
        clean = Game(scenario="door_no_key")
        clean.tick({"use_door": True})
        self.assertFalse(clean.door["open"])
        self.assertIsNone(clean.oracle)
        clean.player.key = True
        clean.tick({"use_door": True})
        self.assertTrue(clean.door["open"])
        self.assertIsNone(clean.oracle)
        buggy = Game(scenario="door_no_key", bug="door_no_key")
        buggy.tick({"use_door": True})
        self.assertEqual(buggy.oracle, "door_without_key")

    def test_clean_and_silent_grenades_keep_warning_evidence_separate(self) -> None:
        from examples.tinyraid.game import Game
        for bug, warned in ((None, True), ("silent_grenade", False)):
            with self.subTest(bug=bug):
                game = Game(scenario="grenade", bug=bug)
                warning_events = [row for row in game.events if row.get("kind") == "grenade_warning"]
                self.assertEqual(bool(warning_events), warned)
                for _ in range(90):
                    game.tick({})
                self.assertEqual(game.outcome, "death")
                death = next(row for row in game.events if row["type"] == "death")
                self.assertEqual(death["cause"], "explosion")
                self.assertEqual(death["warned"], warned)

    def test_crouched_extraction_works_clean_and_times_out_only_with_the_seeded_bug(self) -> None:
        from examples.tinyraid.game import Game
        for bug, expected in ((None, "extract"), ("extract_crouch", "timeout")):
            with self.subTest(bug=bug):
                game = Game(scenario="extraction", bug=bug, duration=2)
                for _ in range(120):
                    game.tick({"extract": True, "stance": "crouch"})
                self.assertEqual(game.outcome, expected)

    def test_wall_visibility_and_closed_door_limit_both_vision_and_navigation(self) -> None:
        from examples.tinyraid.game import Game
        game = Game(scenario="vision")
        self.assertEqual(game.visible_guards(), [])
        self.assertEqual(game.path_to(game.exit_pos), [])
        game.door["open"] = True
        self.assertTrue(game.path_to(game.exit_pos))

    def test_delayed_heal_and_engagement_drop_if_the_action_becomes_illegal(self) -> None:
        from examples.tinyraid.adapter import Adapter
        from examples.tinyraid.game import Guard
        for tactic in ("heal", "engage_g1"):
            with self.subTest(tactic=tactic):
                game = self.game()
                game.player.ammo = 8
                if tactic == "heal":
                    game.player.hp = 50
                else:
                    game.guards = [Guard("g1", 5.5, 2.5)]
                adapter = Adapter(game)
                adapter.decide(adapter.observe(), "start")
                selected = next(row for row in game.events if row["type"] == "decision")
                self.assertEqual(selected["choice"], tactic)
                if tactic == "heal":
                    game.player.heals = 0
                else:
                    game.guards[0].hp = 0
                for _ in range(18):
                    game.tick({})
                adapter.apply_pending()
                receipt = [row for row in game.events if row["type"] == "decision"][-1]
                self.assertEqual(receipt["seq"], selected["seq"])
                self.assertEqual(receipt["choice"], tactic)
                self.assertEqual(receipt["status"], "dropped")
                self.assertEqual(receipt["step"], 18)
                self.assertTrue(receipt["drop_reason"])
                self.assertEqual(adapter.active, "hold")
                self.assertTrue(any(row["tactic"] == tactic and row["ended"] == "dropped"
                                    for row in adapter.observe()["recent"]))
                self.assertFalse(any(row["type"] in {"heal_begin", "shot"} for row in game.events))

    def test_grenade_reflex_waits_for_reaction_and_only_evades_warned_hazards(self) -> None:
        from examples.tinyraid.adapter import Adapter
        from examples.tinyraid.game import Game
        for bug, expected in ((None, "timeout"), ("silent_grenade", "death")):
            with self.subTest(bug=bug):
                game = Game(scenario="grenade", bug=bug, duration=2)
                adapter = Adapter(game)
                origin = game.position
                for _ in range(18):
                    inputs = adapter.execute()
                    self.assertNotIn("move", inputs)
                    game.tick(inputs)
                self.assertEqual(game.position, origin)
                for _ in range(102):
                    game.tick(adapter.execute())
                self.assertEqual(game.outcome, expected)
                self.assertEqual(adapter.active, "hold")
                if bug is None:
                    self.assertEqual(game.player.hp, 100)
                    self.assertGreater(math.dist(origin, game.position), 3)

    def test_idle_no_progress_is_a_warning_and_does_not_stop_the_game(self) -> None:
        from examples.tinyraid.game import Game
        game = Game(scenario="heal_overflow", duration=121)
        # The first simulated step establishes the initial visited cell.
        for _ in range(120 * 60 + 1):
            game.tick({})
        self.assertIsNone(game.outcome)
        self.assertIsNone(game.oracle)
        warnings = [row for row in game.events if row["type"] == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["oracle"], "no_progress")

    def test_guards_hear_actual_shots_but_not_empty_triggers_or_stationary_sprint(self) -> None:
        from examples.tinyraid.game import Game
        for trigger in ("empty-fire", "stationary-sprint", "shot"):
            with self.subTest(trigger=trigger):
                game = Game(scenario="vision")
                self.assertFalse(game.line_of_sight(game.position,
                                                   (game.guards[0].x, game.guards[0].y)))
                if trigger == "empty-fire":
                    game.player.ammo = 0
                    inputs = {"fire": True}
                elif trigger == "stationary-sprint":
                    inputs = {"sprint": True}
                else:
                    inputs = {"fire": True}
                game.tick(inputs)
                shots = [row for row in game.events if row["type"] == "shot"]
                sounds = [row for row in game.events if row["type"] == "sound"]
                if trigger == "shot":
                    self.assertEqual(len(shots), 1)
                    self.assertTrue(sounds)
                    self.assertEqual(game.guards[0].mode, "search")
                else:
                    self.assertEqual(shots, [])
                    self.assertEqual(sounds, [])
                    self.assertEqual(game.guards[0].mode, "patrol")


if __name__ == "__main__":
    unittest.main()
