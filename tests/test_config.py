"""Configuration boundaries and portable argv expansion for M2."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

import tests  # noqa: F401 - make src available without installation

from shakedown.config import build_command, expand_command, load_config


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve() / "project with spaces"
        self.base.mkdir()
        self.path = self.base / "shakedown.toml"

    def load(self, extra="", *, command=None):
        argv = command or ["{python}", "game.py", "--seed", "{seed}"]
        self.path.write_text("[game]\ncommand = " + json.dumps(argv) + "\n" + extra,
                             encoding="utf-8")
        return load_config(self.path)

    def command(self, config, **changes):
        kwargs = dict(run_dir=self.base / "run with spaces", seed=7, map_name="yard",
                      profile="cautious", decider_mode="rule", duration=12,
                      tuning_file=self.base / "run with spaces/tuning.json")
        kwargs.update(changes)
        return build_command(config, **kwargs)

    def test_tinyraid_config_is_a_sixty_run_batch_and_keys_match_the_game(self):
        from examples.tinyraid.game import TUNING_KEYS

        config = load_config(ROOT / "examples/tinyraid/shakedown.toml")
        self.assertEqual(config.root, ROOT)
        self.assertEqual(len(config.seeds) * len(config.maps) * len(config.profiles)
                         * len(config.backends) * len(config.tuning_candidates), 60)
        self.assertEqual(config.backends, ("rule",))
        self.assertEqual(config.checks["oracle_seconds"], 14)
        keys = json.loads((ROOT / "examples/tinyraid/tuning_keys.json").read_text(encoding="utf-8"))
        self.assertEqual(keys["keys"], TUNING_KEYS)

    def test_defaults_are_offline_and_no_shell_is_introduced(self):
        config = self.load()
        self.assertEqual(config.backends, ("mock",))
        self.assertEqual(config.tuning_candidates, ({},))
        self.assertEqual(config.exit_codes, {"ok": 0, "contract": 2, "oracle": 4, "divergence": 5})
        command = self.command(config, map_name="yard; echo should-not-run")
        self.assertEqual(command, [sys.executable, "game.py", "--seed", "7"])
        command = expand_command(config, ["{python}", "{map}", "{profile}"],
                                 run_dir=self.base, seed=1, map_name="yard; echo harmless",
                                 profile="$(do-not-run)", decider_mode="rule", duration=1,
                                 tuning_file=self.base / "tuning.json")
        self.assertEqual(command[1:], ["yard; echo harmless", "$(do-not-run)"])

    def test_relative_root_executable_and_paths_with_spaces_are_single_arguments(self):
        (self.base / "config").mkdir()
        self.path = self.base / "config/settings.toml"
        config = self.load('cwd = ".."\nexe = "build/my game"\n',
                           command=["{exe}", "{project_dir}", "{run_dir}", "{tuning_file}"])
        command = self.command(config)
        self.assertEqual(command, [str(self.base / "build/my game"), str(self.base),
                                   str(self.base / "run with spaces"),
                                   str(self.base / "run with spaces/tuning.json")])
        literal = self.load(command=["build/game", "literal file"])
        self.assertEqual(self.command(literal)[0], str(self.path.parent / "build/game"))

    def test_replay_requires_a_template_and_input_file(self):
        config = self.load()
        with self.assertRaisesRegex(ValueError, "replay_command"):
            self.command(config, replay=True, inputs_file=self.base / "input.json")
        config = self.load('replay_command = ["{python}", "--replay", "{inputs_file}"]\n')
        with self.assertRaisesRegex(ValueError, "inputs_file"):
            self.command(config, replay=True)
        self.assertEqual(self.command(config, replay=True, inputs_file=self.base / "old inputs.json"),
                         [sys.executable, "--replay", str(self.base / "old inputs.json")])

    def test_template_rejects_unknown_attributes_conversions_and_format_specs(self):
        for placeholder in ("{unknown}", "{python.real}", "{map[0]}", "{seed!r}", "{seed:04}", "{}", "{"):
            with self.subTest(placeholder=placeholder), self.assertRaises(ValueError):
                self.load(command=["{python}", placeholder])
        config = self.load(command=["{python}", "{{literal}}"])
        self.assertEqual(self.command(config)[1], "{literal}")
        with self.assertRaisesRegex(ValueError, "requires game.exe"):
            self.load(command=["{exe}"])

    def test_command_arrays_and_unknown_keys_are_validated(self):
        invalid = [
            '[game]\ncommand="python game.py"',
            '[game]\ncommand=[]',
            '[game]\ncommand=["python",7]',
            '[game]\ncommand=["python"]\nmapz=["yard"]',
            '[game]\ncommand=["python"]\n[batch]\nworker=2',
            '[game]\ncommand=["python"]\n[other]\nkey=1',
        ]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.path.write_text(content, encoding="utf-8")
                load_config(self.path)

    def test_batch_numbers_and_duplicate_dimensions_are_not_silently_coerced(self):
        for extra in ('[batch]\nseconds=nan', '[batch]\nseconds=inf', '[batch]\nseconds=0',
                      '[batch]\nseconds=' + '9' * 400, '[batch]\nworkers=true',
                      '[batch]\nbootstrap_samples=0', '[batch]\nseeds=[true]',
                      '[batch]\nseeds=[1,1]', '[batch]\ndeciders=["rule","rule"]',
                      'maps=["yard","yard"]', 'profiles=[]'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.load(extra)

    def test_only_offline_batch_backends_are_accepted(self):
        for extra in ('[decider]\nbackend="jev"', '[batch]\ndeciders=["tape"]',
                      '[batch]\ndeciders=["unknown"]', '[decider]\nbudget_usd=-1'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.load(extra)
        config = self.load('[decider]\nbackend="mock"\n[planner]\nbackend="codex-cli"\nbudget_usd=0\n')
        self.assertEqual(config.backends, ("mock",))

    def test_instruction_and_key_paths_resolve_from_config_not_process_cwd(self):
        (self.base / "instructions.txt").write_text("조심해서 이동", encoding="utf-8")
        config = self.load('[decider]\ninstructions="instructions.txt"\n')
        self.assertEqual(config.instructions, "조심해서 이동")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.load('[decider]\ninstructions="missing.txt"\n')

    def test_fixture_paths_cannot_escape_the_isolated_project_copy(self):
        (self.base / "profile.json").write_text("{}", encoding="utf-8")
        config = self.load('fixtures=["profile.json"]\n')
        self.assertEqual(config.fixtures, ("profile.json",))
        for fixture in ("../outside", "..\\outside", "C:/outside.json", "/outside.json", ".", "missing.json"):
            with self.subTest(fixture=fixture), self.assertRaises(ValueError):
                self.load("fixtures=" + json.dumps([fixture]) + "\n")

    def test_exit_codes_are_distinct_nonnegative_integers(self):
        config = self.load('[game.exit_codes]\noracle=10\n')
        self.assertEqual(config.exit_codes["oracle"], 10)
        for extra in ('[game.exit_codes]\noracle=0', '[game.exit_codes]\ncontract=true',
                      '[game.exit_codes]\nok=-1', '[game.exit_codes]\nunknown=9'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.load(extra)

    def test_tuning_values_follow_the_key_table_and_defaults_remain_empty(self):
        table = {"keys": {"speed": {"type": "float", "min": 1, "max": 5, "default": 3},
                          "count": {"type": "int", "min": 1, "max": 4}}}
        (self.base / "keys.json").write_text(json.dumps(table), encoding="utf-8")
        config = self.load('[tuning]\nkeys="keys.json"\ncandidates=[{}, {speed=4,count=2}]\n')
        self.assertEqual(config.tuning_candidates, ({}, {"speed": 4, "count": 2}))
        for candidate in ('{speed=0}', '{count=1.5}', '{count=true}', '{unknown=2}', '{speed=nan}'):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.load('[tuning]\nkeys="keys.json"\ncandidates=[' + candidate + ']\n')
        with self.assertRaisesRegex(ValueError, "require tuning.keys"):
            self.load('[tuning]\ncandidates=[{speed=3}]\n')
        with self.assertRaisesRegex(ValueError, "duplicate tuning"):
            self.load('[tuning]\ncandidates=[{},{}]\n')

    def test_checks_have_valid_argv_and_timeouts(self):
        config = self.load('[checks]\noracle_args=["--scenario","stuck"]\noracle_seconds=14\n'
                           'visibility_command=["{python}","check.py"]\n')
        self.assertEqual(config.checks["oracle_args"], ["--scenario", "stuck"])
        for extra in ('[checks]\noracle_seconds=inf', '[checks]\noracle_args="shell command"',
                      '[checks]\nvisibility_command=["{unknown}"]'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.load(extra)

    def test_integer_tuning_bounds_are_exact_beyond_float_precision(self):
        boundary = 2 ** 53
        table = {"keys": {"count": {"type": "int", "min": -boundary, "max": boundary}}}
        (self.base / "keys.json").write_text(json.dumps(table), encoding="utf-8")
        for value in (-boundary, boundary):
            with self.subTest(accepted=value):
                config = self.load('[tuning]\nkeys="keys.json"\ncandidates=[{count='
                                   + str(value) + '}]\n')
                self.assertEqual(config.tuning_candidates, ({"count": value},))
        for value in (-boundary - 1, boundary + 1):
            with self.subTest(rejected=value), self.assertRaisesRegex(ValueError, "out-of-range"):
                self.load('[tuning]\nkeys="keys.json"\ncandidates=[{count=' + str(value) + '}]\n')

    def test_oracle_kind_accepts_a_name_and_rejects_empty_or_nonstring_values(self):
        config = self.load('[checks]\noracle_kind="movement_stuck"\n')
        self.assertEqual(config.checks["oracle_kind"], "movement_stuck")
        for value in ('""', 'true', '4', '["stuck"]', '{kind="stuck"}'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "checks.oracle_kind"):
                self.load('[checks]\noracle_kind=' + value + '\n')


if __name__ == "__main__":
    unittest.main()
