"""Process-level contract and replay tests for the M0 bridge."""

from __future__ import annotations

import json
import math
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401 - Add the source tree without requiring an installation.

from shakedown.bridge import run_bridge
from shakedown.deciders import Decision, make_decider
from shakedown.protocol import PREFIX


ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / "tests" / "fake_game.py"


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def without_latency(rows: list[dict]) -> list[dict]:
    return [{key: value for key, value in row.items() if key != "latency_ms"} for row in rows]


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def game_command(self, *, seed: int = 1, decisions: int = 6,
                     telemetry: Path | None = None, fault: str | None = None) -> list[str]:
        command = [sys.executable, str(GAME), "--seed", str(seed), "--decisions", str(decisions)]
        if telemetry is not None:
            command.extend(("--telemetry", str(telemetry)))
        if fault is not None:
            command.extend(("--fault", fault))
        return command

    def bridge(self, name: str, *, decider: str = "mock", seed: int = 1,
               decisions: int = 6, fault: str | None = None,
               tape_path: Path | None = None) -> tuple[int, Path, dict]:
        run_dir = self.base / name
        command = self.game_command(seed=seed, decisions=decisions,
                                    telemetry=run_dir / "telemetry.jsonl", fault=fault)
        run_dir.mkdir()
        backend = make_decider(decider, seed=seed,
                               tape_path=str(tape_path) if tape_path else None)
        code = run_bridge(command, run_dir, backend, decider_name=decider)
        summary = json.loads((run_dir / "bridge_summary.json").read_text(encoding="utf-8"))
        return code, run_dir, summary

    def test_mock_run_writes_tape_summary_and_ordinary_output(self) -> None:
        code, run_dir, summary = self.bridge("mock")
        self.assertEqual(code, 0)
        self.assertEqual(len(records(run_dir / "decision_tape.jsonl")), 6)
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["decisions"], 6)
        self.assertEqual(summary["end"]["outcome"], "extract")
        self.assertEqual(summary["game"]["game"], "fake_game")
        first = records(run_dir / "decision_tape.jsonl")[0]
        names = ["advance", "hold", "reload", "heal", "engage_e1"]
        random.Random(first["order_seed"]).shuffle(names)
        self.assertEqual(list(first["candidates"]), names)
        self.assertIn("fake game started", (run_dir / "stdout.log").read_text(encoding="utf-8"))
        latencies = sorted(row["latency_ms"] for row in records(run_dir / "decision_tape.jsonl"))
        self.assertEqual(summary["latency_ms_p50"], latencies[math.ceil(len(latencies) * .50) - 1])
        self.assertEqual(summary["latency_ms_p95"], latencies[math.ceil(len(latencies) * .95) - 1])

    def test_mock_runs_are_deterministic_except_latency(self) -> None:
        _, first, _ = self.bridge("first", seed=5)
        _, second, _ = self.bridge("second", seed=5)
        self.assertEqual((first / "telemetry.jsonl").read_bytes(),
                         (second / "telemetry.jsonl").read_bytes())
        self.assertEqual(without_latency(records(first / "decision_tape.jsonl")),
                         without_latency(records(second / "decision_tape.jsonl")))

    def test_tape_replay_matches_game_telemetry(self) -> None:
        _, first, _ = self.bridge("original", seed=7)
        code, replay, summary = self.bridge("replay", decider="tape", seed=7,
                                            tape_path=first / "decision_tape.jsonl")
        self.assertEqual(code, 0)
        original_lines = (first / "telemetry.jsonl").read_bytes().splitlines()[1:]
        replay_lines = (replay / "telemetry.jsonl").read_bytes().splitlines()[1:]
        self.assertEqual(original_lines, replay_lines)
        self.assertEqual(summary["decisions"], 6)
        self.assertEqual(summary["sources"], {"tape": 6})

    def _tamper_tape(self, edit) -> Path:
        _, first, _ = self.bridge("original")
        rows = records(first / "decision_tape.jsonl")
        edit(rows)
        path = self.base / "tampered.jsonl"
        path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
                        encoding="utf-8")
        return path

    def _assert_divergence(self, *, tape_path: Path, seed: int = 1) -> None:
        code, _, summary = self.bridge("diverged", decider="tape", seed=seed,
                                       tape_path=tape_path)
        self.assertEqual(code, 5)
        self.assertIsNotNone(summary["divergence"])
        self.assertIn("divergence", summary["invalid_reasons"])
        self.assertFalse(summary["valid"])

    def test_tampered_hash_diverges(self) -> None:
        self._assert_divergence(tape_path=self._tamper_tape(
            lambda rows: rows[0].update(hash="0" * 64)))

    def test_tampered_choice_diverges_at_next_decision(self) -> None:
        def change_choice(rows: list[dict]) -> None:
            row = rows[0]
            row["choice"] = next(name for name in row["candidates"] if name != row["choice"])
        self._assert_divergence(tape_path=self._tamper_tape(change_choice))

    def test_different_game_seed_diverges(self) -> None:
        _, first, _ = self.bridge("original")
        self._assert_divergence(tape_path=first / "decision_tape.jsonl", seed=2)

    def test_malformed_game_requests_are_contract_errors(self) -> None:
        for fault in ("garbage-request", "bad-version", "too-many-candidates", "hello-twice"):
            with self.subTest(fault=fault):
                code, _, summary = self.bridge(fault, fault=fault)
                self.assertEqual(code, 2)
                self.assertTrue(summary["contract_error"])
                self.assertIn("contract_error", summary["invalid_reasons"])
                self.assertFalse(summary["valid"])

    def test_game_rejects_bad_answer_lines_and_eof(self) -> None:
        for answer_kind in ("wrong-seq", "unknown-choice", "broken-json", "eof"):
            with self.subTest(answer_kind=answer_kind):
                with subprocess.Popen(self.game_command(decisions=1),
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) as process:
                    assert process.stdout is not None and process.stdin is not None
                    process.stdout.readline()  # Ordinary output.
                    process.stdout.readline()  # hello.
                    request_line = process.stdout.readline().decode("utf-8")
                    self.assertTrue(request_line.startswith(PREFIX))
                    request = json.loads(request_line[len(PREFIX):])
                    choice = sorted(request["candidates"])[0]
                    if answer_kind == "wrong-seq":
                        answer = {"v": 1, "seq": request["seq"] + 1, "choice": choice}
                    elif answer_kind == "unknown-choice":
                        answer = {"v": 1, "seq": request["seq"], "choice": "unknown"}
                    elif answer_kind == "broken-json":
                        answer = None
                    else:
                        answer = ""
                    if answer is None:
                        process.stdin.write(b"{broken\n")
                    elif isinstance(answer, dict):
                        process.stdin.write((json.dumps(answer) + "\n").encode("utf-8"))
                    process.stdin.close()
                    process.wait(timeout=5)
                    self.assertEqual(process.returncode, 2)
                    assert process.stderr is not None
                    self.assertIn(b"answer contract error", process.stderr.read())

    def test_game_exit_code_passes_through(self) -> None:
        code, _, summary = self.bridge("exit", fault="exit-7")
        self.assertEqual(code, 7)
        self.assertEqual(summary["exit_code"], 7)

    def test_success_without_end_is_invalid(self) -> None:
        code, _, summary = self.bridge("no-end", fault="no-end")
        self.assertEqual(code, 0)
        self.assertIn("no_end_message", summary["invalid_reasons"])
        self.assertFalse(summary["valid"])

    def test_random_run_is_reproducible(self) -> None:
        code1, first, _ = self.bridge("random-1", decider="random", seed=11)
        code2, second, _ = self.bridge("random-2", decider="random", seed=11)
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual((first / "telemetry.jsonl").read_bytes(),
                         (second / "telemetry.jsonl").read_bytes())
        self.assertEqual(without_latency(records(first / "decision_tape.jsonl")),
                         without_latency(records(second / "decision_tape.jsonl")))

    def test_no_decisions_has_null_latency_percentiles(self) -> None:
        code, _, summary = self.bridge("empty", decisions=0)
        self.assertEqual(code, 0)
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["decisions"], 0)
        self.assertIsNone(summary["latency_ms_p50"])
        self.assertIsNone(summary["latency_ms_p95"])

    def test_fallback_rate_boundary_is_strictly_over_ten_percent(self) -> None:
        class FirstFallback:
            def __init__(self) -> None:
                self.calls = 0

            def decide(self, request: dict, instructions: str) -> Decision:
                self.calls += 1
                return Decision(sorted(request["candidates"])[0], {},
                                "fallback" if self.calls == 1 else "mock")

            def close(self) -> None:
                pass

        for decisions, expected_valid in ((10, True), (9, False)):
            with self.subTest(decisions=decisions):
                run_dir = self.base / f"fallback-{decisions}"
                code = run_bridge(self.game_command(decisions=decisions), run_dir,
                                  FirstFallback(), decider_name="custom")
                summary = json.loads((run_dir / "bridge_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(summary["valid"], expected_valid)
                self.assertEqual("fallback_rate_over_10pct" in summary["invalid_reasons"],
                                 not expected_valid)

    def test_wall_timeout_marks_run_invalid(self) -> None:
        run_dir = self.base / "timeout"
        code = run_bridge([sys.executable, "-c", "import time; time.sleep(10)"],
                          run_dir, make_decider("mock"), decider_name="mock",
                          wall_timeout_s=0.1)
        summary = json.loads((run_dir / "bridge_summary.json").read_text(encoding="utf-8"))
        self.assertNotEqual(code, 0)
        self.assertTrue(summary["wall_timeout"])
        self.assertIn("wall_timeout", summary["invalid_reasons"])

    def test_duplicate_sequence_and_late_hello_are_contract_errors(self) -> None:
        for mode, expected in (("duplicate-seq", "seq"), ("late-hello", "hello")):
            with self.subTest(mode=mode):
                script = f"""
import json, sys
prefix = '@@shakedown '
def emit(message):
    print(prefix + json.dumps(message), flush=True)
hello = {{'v':1, 'type':'hello', 'game':'fixture',
         'adapter_version':'1', 'seed':1, 'profile':'cautious'}}
request = {{'v':1, 'type':'decide', 'seq':1, 'step':1,
           'reason':'timer', 'profile':'cautious', 'state':{{}},
           'candidates':{{'hold':'Wait'}}, 'order_seed':1}}
emit(hello)
emit(request)
sys.stdin.readline()
emit(hello if {mode!r} == 'late-hello' else request)
sys.stdin.readline()
"""
                run_dir = self.base / mode
                code = run_bridge([sys.executable, "-c", script], run_dir,
                                  make_decider("mock"), decider_name="mock")
                summary = json.loads((run_dir / "bridge_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(code, 2)
                self.assertIn(expected, summary["contract_error"])

    def test_invalid_decider_result_raises_and_closes_decider(self) -> None:
        class InvalidDecider:
            closed = False

            def decide(self, request: dict, instructions: str) -> Decision:
                return Decision("not_a_candidate", {}, "mock")

            def close(self) -> None:
                self.closed = True

        backend = InvalidDecider()
        run_dir = self.base / "invalid-decider"
        with self.assertRaisesRegex(RuntimeError, "choice"):
            run_bridge(self.game_command(decisions=1), run_dir, backend,
                       decider_name="invalid")
        self.assertTrue(backend.closed)
        summary = json.loads((run_dir / "bridge_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["decisions"], 0)
        self.assertNotEqual(summary["exit_code"], 0)

    def test_cli_invokes_bridge_without_install(self) -> None:
        run_dir = self.base / "cli"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run([sys.executable, "-m", "shakedown", "bridge", "--decider", "mock",
                                 "--run-dir", str(run_dir), "--", *self.game_command(seed=3)],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((run_dir / "bridge_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["decisions"], 6)
        self.assertIn("mock", result.stderr)


if __name__ == "__main__":
    unittest.main()
