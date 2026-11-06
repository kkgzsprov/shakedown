"""Version 1 wire-format and canonical digest conformance tests."""

import copy
import json
import unittest

import tests  # noqa: F401 - installs the repository src directory for discovery

from shakedown.protocol import (
    PREFIX, ProtocolError, canonical_json, encode_answer, parse_answer,
    parse_game_line, request_hash,
)


def example_request() -> dict:
    """The complete request illustrated in the English protocol document."""
    return {
        "v": 1, "type": "decide", "run": "r042", "step": 5412,
        "t": 90.2, "seq": 37, "reason": "new_contact", "profile": "cautious",
        "state": {
            "self": {
                "hp": {"thorax": 72, "legs": 40},
                "bleeding": ["legs"], "stance": "crouch",
            },
            "equipment": {"mag": 11, "spare_mags": 2},
            "seen": [{
                "id": "e2", "bearing": 35, "range_m": 18,
                "exposure": "upper_body", "since_s": 0,
            }],
            "memory": [{"id": "e1", "last_seen_s": 6, "range_m": 31}],
            "cues": [{"kind": "footsteps", "bearing": -80}],
            "objective": {
                "goal": "extract_north", "route_m": 140, "time_left_s": 372,
            },
            "recent": [{
                "tactic": "withdraw", "ended": "failed", "seconds": 5,
                "moved_m": 0, "seconds_ago": 1,
            }],
        },
        "candidates": {
            "engage_e2": "Engage e2. 18 m, upper body exposed, 11 rounds",
            "cover_c1": "Move to cover c1. 6 m, 1.2 s exposed, blocks e2",
            "withdraw": "Withdraw south-west. 25 m",
            "hold": "Keep the current action",
        },
        "order_seed": 991,
    }


def game_line(message: dict) -> str:
    return PREFIX + json.dumps(message, ensure_ascii=False) + "\n"


class HashTests(unittest.TestCase):
    def test_golden_hash(self):
        self.assertEqual(
            request_hash(example_request()),
            "94f68cac7a4fd4d06184d147a4ad6d69f7d211a03c4bb2bd479659107a5296cd",
        )

    def test_excluded_fields_do_not_change_hash(self):
        original = example_request()
        changed = copy.deepcopy(original)
        changed.update(run="other", t=999, seq=99, reason="timer", extra="ignored")
        self.assertEqual(request_hash(original), request_hash(changed))

    def test_each_hashed_field_changes_hash(self):
        original = example_request()
        for field, value in (
            ("state", {"different": 1}), ("candidates", {"hold": "Different"}),
            ("order_seed", 992), ("profile", "other"), ("step", 5413),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = value
                self.assertNotEqual(request_hash(original), request_hash(changed))

    def test_candidate_insertion_order_is_ignored(self):
        original = example_request()
        changed = copy.deepcopy(original)
        changed["candidates"] = dict(reversed(list(changed["candidates"].items())))
        self.assertEqual(request_hash(original), request_hash(changed))

    def test_nan_and_non_json_values_raise_protocol_error(self):
        request = example_request()
        request["state"]["invalid"] = float("nan")
        with self.assertRaises(ProtocolError):
            request_hash(request)
        request["state"]["invalid"] = float("inf")
        with self.assertRaises(ProtocolError):
            request_hash(request)
        with self.assertRaises(ProtocolError):
            canonical_json({"invalid": object()})

    def test_canonical_bytes_sort_nested_keys_and_preserve_utf8(self):
        self.assertEqual(
            canonical_json({"z": {"b": 2, "a": "상태"}, "a": 1}),
            b'{"a":1,"z":{"a":"\xec\x83\x81\xed\x83\x9c","b":2}}',
        )


class GameLineTests(unittest.TestCase):
    def test_ordinary_output_is_not_a_message(self):
        self.assertIsNone(parse_game_line("loading the game\n"))

    def test_valid_message_types(self):
        hello = {
            "v": 1, "type": "hello", "game": "fake_game",
            "adapter_version": "1", "seed": 3, "profile": "cautious",
        }
        end = {"v": 1, "type": "end", "outcome": "extract", "step": 6}
        for message in (hello, example_request(), end):
            with self.subTest(kind=message["type"]):
                self.assertEqual(parse_game_line(game_line(message)), message)

    def test_invalid_messages_raise_protocol_error(self):
        base = example_request()
        violations = [
            PREFIX + "not JSON\n",
            game_line({**base, "v": 2}),
            game_line({key: value for key, value in base.items() if key != "candidates"}),
            game_line({**base, "candidates": {f"c{i}": "action" for i in range(13)}}),
            game_line({**base, "seq": True}),
            game_line({**base, "type": "unknown"}),
        ]
        for line in violations:
            with self.subTest(line=line[:70]), self.assertRaises(ProtocolError):
                parse_game_line(line)

    def test_hello_rejects_missing_and_wrongly_typed_fields(self):
        hello = {
            "v": 1, "type": "hello", "game": "the game",
            "adapter_version": "1", "seed": 3, "profile": "cautious",
        }
        for field in ("game", "adapter_version", "seed", "profile"):
            with self.subTest(field=field, violation="missing"):
                changed = {key: value for key, value in hello.items() if key != field}
                with self.assertRaisesRegex(ProtocolError, field):
                    parse_game_line(game_line(changed))
            with self.subTest(field=field, violation="type"):
                changed = {**hello, field: True if field == "seed" else 42}
                with self.assertRaisesRegex(ProtocolError, field):
                    parse_game_line(game_line(changed))

    def test_decide_rejects_bad_fields(self):
        base = example_request()
        violations = (
            ({**base, "candidates": {}}, "candidates"),
            ({**base, "candidates": {"hold": 4}}, "candidates"),
            ({**base, "step": -1}, "step"),
            ({**base, "order_seed": True}, "order_seed"),
            ({**base, "run": 1}, "run"),
            ({**base, "t": True}, "t"),
            ({**base, "t": float("inf")}, "JSON"),
            ({**base, "t": 10**400}, "t"),
        )
        for message, field in violations[:-1]:
            with self.subTest(field=field, value=message.get(field)):
                with self.assertRaises(ProtocolError):
                    parse_game_line(game_line(message))
        self.assertEqual(parse_game_line(game_line(violations[-1][0]))["t"], 10**400)

    def test_end_rejects_invalid_fields(self):
        base = {"v": 1, "type": "end", "outcome": "extract", "step": 6}
        violations = (
            ({"v": 1, "type": "end", "step": 6}, "outcome"),
            ({**base, "outcome": "other"}, "outcome"),
            ({**base, "step": True}, "step"),
            ({**base, "oracle": 3}, "oracle"),
        )
        for message, field in violations:
            with self.subTest(field=field), self.assertRaisesRegex(ProtocolError, field):
                parse_game_line(game_line(message))


class AnswerTests(unittest.TestCase):
    def test_valid_answer_round_trip(self):
        request = example_request()
        line = encode_answer(37, "hold", "mock", {})
        self.assertEqual(parse_answer(line, request)["choice"], "hold")
        self.assertTrue(line.endswith("\n"))

    def test_invalid_answer_lines_raise_protocol_error(self):
        request = example_request()
        lines = [
            encode_answer(38, "hold", "mock", {}),
            encode_answer(37, "unknown", "mock", {}),
            "not JSON\n",
            encode_answer(37, "hold", "mock", {}).rstrip("\n"),
            '{"v":2,"seq":37,"choice":"hold"}\n',
            '[]\n',
        ]
        for line in lines:
            with self.subTest(line=line), self.assertRaises(ProtocolError):
                parse_answer(line, request)

    def test_unknown_source_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "source"):
            encode_answer(1, "hold", "unknown", {})
        with self.assertRaisesRegex(ProtocolError, "source"):
            encode_answer(1, "hold", [], {})


if __name__ == "__main__":
    unittest.main()
