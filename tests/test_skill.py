"""Offline behavior checks for the distributable adapter skill and its helpers."""

import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import tests  # noqa: F401 - Make source imports available without installation.

from shakedown.protocol import PREFIX, ProtocolError, encode_answer, parse_game_line, request_hash
from shakedown.config import build_command, load_config


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill/shakedown"
TEMPLATE = SKILL / "assets/adapter_python.py"
WRAPPER = SKILL / "scripts/run_shakedown.py"


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class FlushedOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shakedown skill ")
        self.addCleanup(tests.cleanup_temp_directory, self.temporary)
        self.root = Path(self.temporary.name)
        self.adapter = module(TEMPLATE, "shakedown_skill_adapter")
        self.environment = dict(os.environ)
        self.environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))

    def process(self, *arguments, answer=""):
        return subprocess.run([sys.executable, str(TEMPLATE), *arguments], input=answer,
                              capture_output=True, text=True, encoding="utf-8",
                              env=self.environment, cwd=self.root, timeout=10)

    def test_python_template_flushed_utf8_messages_and_legal_choice(self):
        writer = FlushedOutput()
        session = self.adapter.WireSession(seed=7, profile="cautious", game="test",
            reader=io.StringIO(encode_answer(1, "hold", "mock", {})), writer=writer)
        with self.assertRaises(ProtocolError):
            session.choose(step=0, state={}, candidates={"hold": "Wait"}, order_seed=1, reason="timer")
        session.start()
        choice, sequence = session.choose(step=12, state={"seen": [], "name": "보이는 세계"},
            candidates={"hold": "Keep the current action"}, order_seed=17, reason="timer")
        self.assertEqual((choice, sequence), ("hold", 1))
        session.finish(outcome="timeout", step=13)
        lines = writer.getvalue().splitlines()
        messages = [parse_game_line(line) for line in lines]
        self.assertEqual([row["type"] for row in messages], ["hello", "decide", "end"])
        self.assertEqual(writer.flushes, 3)
        self.assertIn("보이는 세계", lines[1])
        self.assertEqual(request_hash(messages[1]), request_hash(dict(messages[1], run="another-run", seq=99)))
        with self.assertRaises(ProtocolError):
            session.start()
        with self.assertRaises(ProtocolError):
            session.finish(outcome="timeout", step=13)

    def test_python_template_process_rejects_invalid_answers_immediately(self):
        valid = self.process(answer=encode_answer(1, "hold", "mock", {}))
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual([parse_game_line(line)["type"] for line in valid.stdout.splitlines()],
                         ["hello", "decide", "end"])
        for answer in (encode_answer(2, "hold", "mock", {}),
                       encode_answer(1, "invented", "mock", {}), "{broken\n",
                       '{"v":true,"seq":1,"choice":"hold"}\n',
                       '{"v":1,"seq":1,"choice":"hold"}'):
            with self.subTest(answer=answer):
                result = self.process(answer=answer)
                self.assertEqual(result.returncode, 2)
                self.assertEqual([parse_game_line(line)["type"] for line in result.stdout.splitlines()],
                                 ["hello", "decide"])

    def test_python_template_tape_matches_request_and_reports_divergence(self):
        original = self.process(answer=encode_answer(1, "hold", "mock", {}))
        request = parse_game_line(original.stdout.splitlines()[1])
        tape = self.root / "recorded decisions.jsonl"
        record = {"hash": request_hash(request), "choice": "hold", "probabilities": {}}
        tape.write_text(json.dumps(record) + "\n", encoding="utf-8")
        result = self.process("--mode", f"tape:{tape}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        record["hash"] = "0" * 64
        tape.write_text(json.dumps(record) + "\n", encoding="utf-8")
        result = self.process("--mode", f"tape:{tape}")
        self.assertEqual(result.returncode, 5)
        self.assertIn("request hash", result.stderr)

    def test_python_rule_uses_game_callback_and_rejects_illegal_choice(self):
        calls = []
        session = self.adapter.WireSession(seed=1, profile="careful", game="test", mode="rule",
            rule=lambda request: calls.append(request) or "hold", writer=io.StringIO())
        session.start()
        self.assertEqual(session.choose(step=0, state={}, candidates={"hold": "Wait"},
                                        order_seed=1, reason="turn"), ("hold", 1))
        self.assertEqual(calls[0]["step"], 0)
        session.rule = lambda request: "not-legal"
        with self.assertRaises(ProtocolError):
            session.choose(step=1, state={}, candidates={"hold": "Wait"}, order_seed=1, reason="turn")

    def test_wrapper_preserves_literal_arguments_environment_and_exit_status(self):
        wrapper = module(WRAPPER, "shakedown_skill_wrapper")
        literal = "an output $(never execute); with spaces"
        with patch.object(wrapper.subprocess, "run") as execute:
            execute.return_value.returncode = 130
            code = wrapper.main(["--repo", str(ROOT), "campaign", "--output", literal, "--optimize"])
        self.assertEqual(code, 130)
        argv = execute.call_args.args[0]
        self.assertEqual(argv, [sys.executable, "-m", "shakedown", "campaign", "--output", literal, "--optimize"])
        self.assertNotIn("cwd", execute.call_args.kwargs)
        self.assertNotIn("shell", execute.call_args.kwargs)
        self.assertNotIn("--approved", argv)
        self.assertTrue(execute.call_args.kwargs["env"]["PYTHONPATH"].startswith(str(ROOT / "src")))

    def test_copied_wrapper_runs_offline_plan_from_another_directory(self):
        copied = self.root / "copied skill/scripts/run_shakedown.py"
        copied.parent.mkdir(parents=True)
        shutil.copyfile(WRAPPER, copied)
        design = self.root / "design notes.md"
        design.write_text("Measure the configured baseline; do not apply changes.", encoding="utf-8")
        output = self.root / "draft output"
        result = subprocess.run([sys.executable, str(copied), "--repo", str(ROOT),
            "plan", "targets", "--config", str(ROOT / "examples/tinyraid/shakedown.toml"),
            "--input", f"design_notes={design}", "--output", str(output), "--backend", "mock"],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue((output / "targets.json").is_file())
        self.assertFalse((output / "targets-approved.json").exists())

    def test_visibility_fixture_requires_hidden_negative_and_visible_positive_controls(self):
        fixture = module(SKILL / "assets/visibility_fixture.py", "shakedown_visibility_fixture")
        visible = []
        observe = lambda: {"seen": [{"id": identity} for identity in visible]}
        result = fixture.visibility_fixture(observe, lambda: visible.append("e1"), "e1")
        self.assertTrue(result["passed"])
        self.assertFalse(fixture.visibility_fixture(observe, lambda: None, "e1")["passed"])
        visible.clear()
        self.assertFalse(fixture.visibility_fixture(observe, lambda: None, "e1")["passed"])

    def test_skill_markdown_links_resolve_within_distributable_folder(self):
        for source in SKILL.rglob("*.md"):
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", source.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("#"):
                    continue
                path = (source.parent / target.split("#", 1)[0]).resolve()
                with self.subTest(source=source.name, target=target):
                    self.assertTrue(path.is_relative_to(SKILL.resolve()))
                    self.assertTrue(path.is_file())

    def test_complete_config_parses_and_expands_against_an_isolated_game_root(self):
        example = SKILL / "assets/shakedown.toml"
        text = example.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
        self.assertEqual(set(parsed), {"game", "batch", "decider", "tuning", "checks"})
        source_config = load_config(example)
        self.assertEqual(source_config.root, ROOT)
        self.assertEqual(source_config.maps, ("depot", "loop"))
        game_root = self.root / "isolated game root"
        game_root.mkdir()
        game_root = game_root.resolve()
        shutil.copyfile(ROOT / "examples/switchyard/tuning_keys.json", game_root / "tuning_keys.json")
        config_dir = self.root / "configuration"
        config_dir.mkdir()
        text = text.replace('cwd = "../../.."', 'cwd = ' + json.dumps(str(game_root)))
        text = text.replace('keys = "../../../examples/switchyard/tuning_keys.json"',
                            'keys = ' + json.dumps(str(game_root / "tuning_keys.json")))
        copied = config_dir / "shakedown.toml"
        copied.write_text(text, encoding="utf-8")
        self.assertFalse(copied.read_bytes().startswith(b"\xef\xbb\xbf"))
        config = load_config(copied)
        self.assertEqual(config.root, game_root)
        run_dir = (self.root / "evidence directory").resolve()
        tuning_file = run_dir / "tuning.json"
        inputs_file = run_dir / "inputs.json"
        values = dict(run_dir=run_dir, seed=7, map_name="depot", profile="courier",
                      decider_mode="bridge", duration=16, tuning_file=tuning_file)
        command = build_command(config, **values)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[command.index("--tuning") + 1], str(tuning_file))
        self.assertEqual(command[command.index("--decider") + 1], "bridge")
        replay = build_command(config, **values, inputs_file=inputs_file, replay=True)
        self.assertEqual(replay[replay.index("--replay-inputs") + 1], str(inputs_file))
        self.assertFalse(any("{" in argument or "}" in argument for argument in command + replay))


if __name__ == "__main__":
    unittest.main()
