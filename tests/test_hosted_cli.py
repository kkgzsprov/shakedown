"""Hosted CLI preserves configured policy and reports invalid bridge outcomes."""

import contextlib
import importlib
import io
import json
import sys
import unittest
from unittest.mock import patch, sentinel

import tests
from tests import test_hosted_pipeline as fixtures
from shakedown import bridge, cli


class HostedCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HostedPipelineTests(
            methodName="test_tinyraid_uses_hosted_bridge_with_local_provider_only")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.server = self.fixture.server

    def configured(self):
        config = self.fixture.config(seeds=(1,), workers=1)
        instructions = self.root / "configured-instructions.txt"
        instructions.write_text("Keep the configured cautious policy.", encoding="utf-8")
        text = config.path.read_text(encoding="utf-8")
        text = text.replace('backend = "jev"', 'backend = "jev"\ninstructions = ' + json.dumps(str(instructions)))
        text = text.replace("wall_timeout_s = 5", "wall_timeout_s = 7.5")
        config.path.write_text(text, encoding="utf-8")
        return config.path

    def invoke(self, arguments):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(list(map(str, arguments)))

    @staticmethod
    def bridge_result(*, valid=True, code=0):
        def complete(_command, run_dir, _decider, **_kwargs):
            run_dir.mkdir(parents=True)
            (run_dir / "bridge_summary.json").write_text(json.dumps(
                {"decisions": 2, "valid": valid, "fallback_rate": 0 if valid else 1}), encoding="utf-8")
            return code
        return complete

    def test_hosted_bridge_inherits_configured_instructions_timeout_and_strips_keys(self):
        config = self.configured()
        output = self.root / "configured-bridge"
        with patch.object(cli, "make_decider", return_value=sentinel.decider) as factory, \
             patch.object(cli, "run_bridge", side_effect=self.bridge_result()) as execute:
            result = self.invoke(["bridge", "--decider", "jev", "--config", config,
                "--run-dir", output, "--", sys.executable, "-c", "pass"])
        self.assertEqual(result, 0)
        self.assertEqual(execute.call_args.kwargs["instructions"], "Keep the configured cautious policy.")
        self.assertEqual(execute.call_args.kwargs["wall_timeout_s"], 7.5)
        self.assertEqual(execute.call_args.kwargs["decider_name"], "jev")
        self.assertNotIn(fixtures.KEY_ENV, execute.call_args.kwargs["env"])
        self.assertNotIn("TYPESAFE_API_KEY", execute.call_args.kwargs["env"])
        self.assertEqual(factory.call_args.kwargs["hosted_runtime"].model, "jev-1.14.0")
        self.assertEqual(self.server.requests, [])

    def test_explicit_bridge_instructions_and_timeout_override_config_defaults(self):
        config = self.configured()
        override = self.root / "override.txt"
        override.write_text("Explicit operator instructions.", encoding="utf-8")
        with patch.object(cli, "make_decider", return_value=sentinel.decider), \
             patch.object(cli, "run_bridge", side_effect=self.bridge_result()) as execute:
            result = self.invoke(["bridge", "--decider", "jev", "--config", config,
                "--run-dir", self.root / "override-bridge", "--instructions", override,
                "--wall-timeout", ".25", "--", sys.executable, "-c", "pass"])
        self.assertEqual(result, 0)
        self.assertEqual(execute.call_args.kwargs["instructions"], "Explicit operator instructions.")
        self.assertEqual(execute.call_args.kwargs["wall_timeout_s"], .25)

    def test_invalid_summary_changes_zero_game_exit_to_cli_failure(self):
        config = self.configured()
        for valid, expected in ((True, 0), (False, 1)):
            with self.subTest(valid=valid), \
                 patch.object(cli, "make_decider", return_value=sentinel.decider), \
                 patch.object(cli, "run_bridge", side_effect=self.bridge_result(valid=valid)):
                result = self.invoke(["bridge", "--decider", "jev", "--config", config,
                    "--run-dir", self.root / f"summary-{valid}", "--", sys.executable, "-c", "pass"])
                self.assertEqual(result, expected)

    def test_actual_local_http_fallback_bridge_has_nonzero_cli_exit(self):
        config = self.configured()
        self.server.mode = "fallback"
        output = self.root / "actual-fallback"
        result = self.invoke(["bridge", "--decider", "jev", "--config", config,
            "--run-dir", output, "--", sys.executable, str(fixtures.ROOT / "tests/fake_game.py"),
            "--decider-mode", "bridge", "--decisions", "2", "--telemetry", output / "telemetry.jsonl"])
        summary = fixtures.HostedPipelineTests.read(output / "bridge_summary.json")
        self.assertEqual(result, 1)
        self.assertEqual(summary["exit_code"], 0)
        self.assertFalse(summary["valid"])
        self.assertEqual(summary["fallback_rate"], 1)
        self.assertIn("fallback_rate_over_10pct", summary["invalid_reasons"])
        self.assertEqual(len(self.server.requests), 2)
        self.fixture.assert_no_secret()

    def test_hosted_model_and_budget_preflight_prevents_process_and_output(self):
        changes = ((lambda text: text.replace('model = "jev-1.14.0"\n', "")),
                   (lambda text: text.replace('model = "jev-1.14.0"', 'model = "jev-latest"')),
                   (lambda text: text.replace("budget_usd = 0.1", "budget_usd = 0")))
        for index, mutate in enumerate(changes):
            config = self.configured()
            config.write_text(mutate(config.read_text(encoding="utf-8")), encoding="utf-8")
            output = self.root / f"preflight-{index}"
            with self.subTest(index=index), patch.object(cli, "run_bridge") as execute, \
                 self.assertRaises(SystemExit) as error:
                self.invoke(["bridge", "--decider", "jev", "--config", config,
                    "--run-dir", output, "--", sys.executable, "-c", "pass"])
            self.assertEqual(error.exception.code, 2)
            execute.assert_not_called()
            self.assertFalse(output.exists())
        self.assertEqual(self.server.requests, [])

    def test_explore_shares_runtime_and_skips_unstarted_runs_after_budget_exhaustion(self):
        module = importlib.import_module("shakedown.explore")
        config = self.fixture.config(seeds=(1, 2), workers=2, budget_usd=.004)
        runtimes = []
        factory = module.hosted_runtime
        def shared(*args, **kwargs):
            runtime = factory(*args, **kwargs)
            runtimes.append(runtime)
            return runtime
        with patch.object(module, "hosted_runtime", side_effect=shared) as initialize, \
             patch.object(module, "run_case", wraps=module.run_case) as case, \
             patch.object(bridge, "start_process", wraps=bridge.start_process) as start:
            result = module.explore(config, self.root / "exploration", max_runs=4, seed=7)
        self.assertEqual(initialize.call_count, 1)
        self.assertEqual(case.call_count, 2)
        self.assertEqual(start.call_count, 2)
        self.assertTrue(all(call.kwargs["hosted"] is runtimes[0] for call in case.call_args_list))
        self.assertEqual((result["status"], result["stop_reason"]), ("completed", "decider_budget"))
        self.assertEqual((result["total_runs"], result["valid_runs"], result["invalid_runs"]), (2, 2, 0))
        self.assertEqual(len(result["plan"]), 4)
        self.assertEqual(len(result["skipped_runs"]), 2)
        self.assertEqual({row["reason"] for row in result["skipped_runs"]}, {"not_started_budget"})
        self.assertEqual(len(self.server.requests), 4)
        self.assertEqual(result["hosted"]["http_attempts"], 4)
        self.assertEqual(result["hosted"]["budget"]["remaining_usd"], "0")
        self.fixture.assert_no_secret()


if __name__ == "__main__":
    unittest.main()
