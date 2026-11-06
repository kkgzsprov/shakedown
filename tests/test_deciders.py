"""Offline decider determinism and tape divergence tests."""

import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401 - installs the repository src directory for discovery

from shakedown.deciders import make_decider
from shakedown.deciders.mock import MockDecider
from shakedown.deciders.random import RandomDecider
from shakedown.deciders.tape import TapeDecider, TapeDivergence, read_tape
from shakedown.protocol import request_hash


def request(step: int = 1) -> dict:
    return {
        "v": 1, "type": "decide", "seq": step, "step": step,
        "reason": "timer", "profile": "cautious", "state": {"x": step},
        "candidates": {"hold": "Wait", "advance": "Move", "reload": "Reload"},
        "order_seed": 4,
    }


class LocalDeciderTests(unittest.TestCase):
    def test_random_is_request_local_and_repeatable(self):
        decider = RandomDecider(5)
        first = decider.decide(request(), "instruction")
        decider.decide(request(2), "other instruction")
        self.assertEqual(first, decider.decide(request(), ""))
        self.assertEqual(first.source, "random")
        self.assertEqual(first.probabilities, {})

    def test_random_seed_changes_choices_over_request_set(self):
        choices_a = [RandomDecider(5).decide(request(step), "").choice for step in range(1, 30)]
        choices_b = [RandomDecider(6).decide(request(step), "").choice for step in range(1, 30)]
        self.assertNotEqual(choices_a, choices_b)

    def test_mock_is_deterministic(self):
        decider = MockDecider()
        self.assertEqual(decider.decide(request(), "a"), decider.decide(request(), "b"))
        self.assertEqual(decider.decide(request(), "").source, "mock")

    def test_factory_selects_local_deciders(self):
        self.assertIsInstance(make_decider("mock"), MockDecider)
        self.assertIsInstance(make_decider("random", seed=3), RandomDecider)
        with self.assertRaises(ValueError):
            make_decider("tape")


class TapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "decision_tape.jsonl"
        self.first = request()
        self.record = {
            "hash": request_hash(self.first), "choice": "hold",
            "probabilities": {"hold": 1.0},
        }
        self.write_record(self.record)

    def write_record(self, record):
        self.path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_tape_replays_recorded_choice(self):
        self.assertEqual(read_tape(self.path), [self.record])
        decision = TapeDecider(self.path).decide(self.first, "")
        self.assertEqual(decision.choice, "hold")
        self.assertEqual(decision.source, "tape")
        self.assertEqual(decision.probabilities, {"hold": 1.0})

    def test_hash_mismatch_reports_both_hashes(self):
        changed = request(2)
        with self.assertRaises(TapeDivergence) as captured:
            TapeDecider(self.path).decide(changed, "")
        self.assertIn(self.record["hash"], str(captured.exception))
        self.assertIn(request_hash(changed), str(captured.exception))
        self.assertIn("seq 2", str(captured.exception))

    def test_exhaustion_raises_divergence(self):
        decider = TapeDecider(self.path)
        decider.decide(self.first, "")
        with self.assertRaisesRegex(TapeDivergence, "exhausted"):
            decider.decide(request(2), "")

    def test_illegal_recorded_choice_raises_divergence(self):
        self.write_record({**self.record, "choice": "unknown"})
        with self.assertRaisesRegex(TapeDivergence, "choice"):
            TapeDecider(self.path).decide(self.first, "")

    def test_unhashable_recorded_choice_raises_divergence(self):
        self.write_record({**self.record, "choice": []})
        with self.assertRaisesRegex(TapeDivergence, "choice"):
            TapeDecider(self.path).decide(self.first, "")


if __name__ == "__main__":
    unittest.main()
