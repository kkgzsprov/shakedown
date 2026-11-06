"""The exposed tuning table is tinyraid's runtime source of defaults."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from examples.tinyraid import game


class TinyraidDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tuning_keys.json"

    def load(self, keys):
        self.path.write_text(json.dumps({"keys": keys}), encoding="utf-8")
        return game.load_tuning_keys(self.path)

    def test_checked_in_table_preserves_current_bounds_and_defaults(self):
        expected = {
            "player.speed": {"type": "float", "min": 1.0, "max": 5.0, "default": 3.0},
            "guard.damage": {"type": "float", "min": 1.0, "max": 20.0, "default": 8.0},
            "guard.vision": {"type": "float", "min": 3.0, "max": 16.0, "default": 10.0},
        }
        self.assertEqual(game.TUNING_KEYS, expected)
        self.assertEqual(game.Game().tuning, {key: value["default"] for key, value in expected.items()})

    def test_loaded_default_changes_reach_game_without_overwriting_explicit_values(self):
        keys = deepcopy(game.TUNING_KEYS)
        keys["player.speed"]["default"] = 4.0
        loaded = self.load(keys)
        with patch.object(game, "TUNING_KEYS", loaded):
            self.assertEqual(game.Game().tuning["player.speed"], 4.0)
            self.assertEqual(game.Game(tuning={"player.speed": 2.0}).tuning["player.speed"], 2.0)
            with self.assertRaises(ValueError):
                game.Game(tuning={"player.speed": 6.0})

    def test_integer_table_values_remain_exact_and_fractional_overrides_fail(self):
        low = 2 ** 53 + 1
        keys = deepcopy(game.TUNING_KEYS)
        keys["player.speed"] = {"type": "int", "min": low, "max": low + 2, "default": low + 1}
        loaded = self.load(keys)
        self.assertEqual(loaded["player.speed"]["default"], low + 1)
        with patch.object(game, "TUNING_KEYS", loaded):
            self.assertEqual(game.Game().tuning["player.speed"], low + 1)
            with self.assertRaises(ValueError):
                game.Game(tuning={"player.speed": float(low + 1)})

    def test_invalid_specifications_are_rejected(self):
        valid = {"type": "float", "min": 1, "max": 5, "default": 3}
        for changes in ({"type": "number"}, {"type": []}, {"min": True}, {"max": float("inf")},
                        {"default": float("nan")}, {"min": 6}, {"default": 6},
                        {"type": "int", "default": 2.5}, {"type": "int", "min": 1.0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.load({"speed": {**valid, **changes}})
        for value in ({}, {"speed": []}, {"": valid}, {"speed": {"type": "float", "min": 1, "max": 5}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)

    def test_duplicate_json_keys_and_overflowing_numbers_are_rejected(self):
        for text in ('{"keys":{},"keys":{}}',
                     '{"keys":{"speed":{"type":"float","min":1,"min":2,"max":5,"default":3}}}',
                     '{"keys":{"speed":{"type":"float","min":1,"max":1e999,"default":3}}}',
                     '{"keys":[]}', '{}'):
            self.path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                game.load_tuning_keys(self.path)


if __name__ == "__main__":
    unittest.main()
