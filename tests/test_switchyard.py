"""A second game proves the thin executor, replay, and adapter contract."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests

from examples.switchyard.__main__ import event_hash, main, read_recording
from examples.switchyard.adapter import Adapter
from examples.switchyard.checks import visibility_fixture
from examples.switchyard.game import Game, validate_config
from shakedown.config import load_config
from shakedown.conformance import check_adapter


ROOT = Path(__file__).resolve().parents[1]


def config(**changes):
    value = {"seed": 7, "map_name": "depot", "duration": 16, "profile": "courier",
             "scenario": "standard", "tuning": {}}
    value.update(changes)
    return value


class SwitchyardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="switchyard-")
        self.addCleanup(tests.cleanup_temp_directory, self.temporary)
        self.root = Path(self.temporary.name)

    def test_ordinary_buttons_collect_and_deliver_with_no_bot_state_writes(self):
        game = Game(config())
        for button in ("east", "collect", "east", "east", "south", "south", "south", "east", "deliver"):
            game.tick({"button": button})
        self.assertEqual((game.outcome, game.cargo, game.loot, game.position), ("extract", 1, 1, (4, 3)))
        self.assertEqual(game.events[-1]["counts"]["loot"], 1)
        with self.assertRaises(ValueError):
            game.tick({"button": "wait"})

    def test_rule_uses_the_same_button_path_on_both_maps(self):
        for map_name in ("depot", "loop"):
            for profile in ("courier", "careful"):
                with self.subTest(map=map_name, profile=profile):
                    game = Game(config(map_name=map_name, profile=profile))
                    adapter = Adapter(game, "rule")
                    while game.outcome is None:
                        event = adapter.tick()
                        self.assertEqual(set(event), {"button"})
                        game.tick(event)
                    self.assertEqual(game.outcome, "extract")
                    self.assertEqual(sum(row["type"] == "decision" and row["status"] == "selected"
                                         for row in game.events), game.step)

    def test_illegal_inputs_and_tuning_do_not_move_the_player(self):
        game = Game(config())
        for event in ({"button": "west"}, {"button": "collect"}, {"button": "deliver"},
                      {"button": "east", "teleport": [4, 3]}, {"button": []}):
            with self.subTest(event=event), self.assertRaises(ValueError):
                game.tick(event)
            self.assertEqual((game.step, game.position), (0, (0, 0)))
        for tuning in ({"cargo.value": True}, {"cargo.value": 4}, {"unknown": 1}):
            with self.subTest(tuning=tuning), self.assertRaises(ValueError):
                validate_config(config(tuning=tuning))
        reward = Game(config(tuning={"cargo.value": 3}))
        reward.tick({"button": "east"})
        reward.tick({"button": "collect"})
        self.assertEqual(reward.loot, 3)

    def test_unseen_maintenance_identity_and_behind_wall_position_do_not_leak(self):
        game = Game(config())
        game.position = (1, 1)
        game.hidden_hazards = {(3, 1): 123456}
        before = Adapter(game, "rule").request()
        game.hidden_hazards = {(4, 3): 987654}
        self.assertEqual(before, Adapter(game, "rule").request())
        self.assertEqual(before["state"]["seen"], [])
        game.position = (4, 2)
        self.assertEqual(game.observation()["seen"][0]["position"], [4, 3])
        self.assertNotIn("987654", json.dumps(game.observation()))
        self.assertTrue(visibility_fixture()["passed"])

    def test_recorded_inputs_replay_without_constructing_an_adapter(self):
        recording, original, replayed = self.root / "inputs.json", self.root / "original.jsonl", self.root / "replay.jsonl"
        self.assertEqual(main(["--seed", "13", "--record-inputs", str(recording), "--telemetry", str(original)]), 0)
        data = read_recording(recording)
        self.assertEqual(len(data["inputs"]), data["result"]["step"])
        with patch("examples.switchyard.__main__.Adapter", side_effect=AssertionError("replay consulted adapter")):
            self.assertEqual(main(["--replay-inputs", str(recording), "--decider", "invalid", "--telemetry", str(replayed)]), 0)
        rows = [json.loads(line) for line in original.read_text().splitlines()]
        other = [json.loads(line) for line in replayed.read_text().splitlines()]
        self.assertEqual([row for row in rows if row["type"] != "decision"], other)
        self.assertEqual(event_hash(rows), event_hash(other))

    def test_recording_rejects_missing_turns_and_detects_changed_valid_result(self):
        recording = self.root / "inputs.json"
        self.assertEqual(main(["--record-inputs", str(recording)]), 0)
        data = read_recording(recording)
        broken = deepcopy(data)
        broken["inputs"].pop()
        recording.write_text(json.dumps(broken))
        with self.assertRaises(ValueError):
            read_recording(recording)
        changed = deepcopy(data)
        changed["result"]["events_hash"] = "0" * 64
        recording.write_text(json.dumps(changed))
        self.assertEqual(main(["--replay-inputs", str(recording)]), 3)
        changed = deepcopy(data)
        changed["inputs"][0]["t"] = .5
        recording.write_text(json.dumps(changed))
        self.assertEqual(main(["--replay-inputs", str(recording)]), 2)

    def test_stuck_fixture_records_an_oracle_and_reproduces_it(self):
        recording = self.root / "oracle-inputs.json"
        self.assertEqual(main(["--scenario", "stuck", "--duration", "8", "--record-inputs", str(recording)]), 4)
        data = read_recording(recording)
        self.assertEqual(data["result"]["oracle"], "stuck")
        self.assertEqual([row["input"] for row in data["inputs"]], [{"button": "wait"}] * 4)
        self.assertEqual(main(["--replay-inputs", str(recording)]), 4)

    def test_real_adapter_passes_all_seven_conformance_checks(self):
        report = check_adapter(load_config(ROOT / "examples/switchyard/shakedown.toml"), self.root / "conformance")
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual({name: row["status"] for name, row in report["checks"].items()},
                         {f"C{number}": "passed" for number in range(1, 8)})
        self.assertTrue(report["checks"]["C7"]["fixture"]["behind_wall_not_seen"])


if __name__ == "__main__":
    unittest.main()
