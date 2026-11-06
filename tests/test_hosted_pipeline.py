"""Hosted decisions through real local HTTP, bridge and batch artifacts only."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import tests
from shakedown import bridge
from shakedown.config import load_config
from shakedown.orchestrate import hosted_runtime, run_batch, run_case
from shakedown.planner.campaign import run_approved_campaign


ROOT = Path(__file__).resolve().parents[1]
KEY_ENV = "HOSTED_PIPELINE_TEST_KEY"
SECRET = "hosted-pipeline-fixture-secret-not-a-real-key"


class ProviderFixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.mode = "success"
        self.requests = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fixture.lock:
                    fixture.requests.append({"body": request, "authorization": self.headers.get("Authorization")})
                    mode = fixture.mode
                if mode in {"fatal", "fallback"}:
                    self.send_response(400 if mode == "fatal" else 503)
                    self.end_headers()
                    self.wfile.write(b'{"error":"fixture only"}')
                    return
                names = sorted(request["questions"]["tactic"]["criteria"])
                response = {"model": "jev-9.9.9" if mode == "drift" else request["model"],
                    "answers": {"tactic": {"type": "choice", "choice": names[0], "confidence": 1,
                        "probabilities": {name: int(name == names[0]) for name in names}}},
                    "usage": {"input_tokens": 32, "output_tokens": 2}}
                body = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class HostedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hosted pipeline ")
        self.addCleanup(tests.cleanup_temp_directory, self.temporary)
        self.root = Path(self.temporary.name)
        self.server = ProviderFixture()
        self.addCleanup(self.server.close)
        self.environment = patch.dict(os.environ, {KEY_ENV: SECRET, "TYPESAFE_API_KEY": SECRET})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.index = 0

    def config(self, *, game="fixture", seeds=(1, 2), duration=.1, workers=2, **overrides):
        self.index += 1
        project = self.root / f"project-{self.index}"
        project.mkdir()
        if game == "tinyraid":
            command = ["{python}", "-m", "examples.tinyraid", "--headless", "--seed", "{seed}",
                "--map", "{map}", "--duration", "{seconds}", "--telemetry", "{run_dir}/telemetry.jsonl",
                "--record-inputs", "{run_dir}/inputs.json", "--decider", "{decider_mode}",
                "--profile-name", "{profile}", "--tuning", "{tuning_file}"]
            cwd = ROOT
        else:
            wrapper = project / "fixture.py"
            wrapper.write_text("import json, os, runpy\nfrom pathlib import Path\n"
                "Path('environment-check.json').write_text(json.dumps({"
                f"'custom_key_present': {KEY_ENV!r} in os.environ, "
                "'default_key_present': 'TYPESAFE_API_KEY' in os.environ}))\n"
                f"runpy.run_path({str(ROOT / 'tests/batch_game.py')!r}, run_name='__main__')\n",
                encoding="utf-8")
            command = ["{python}", str(wrapper), "--seed", "{seed}", "--map", "{map}",
                "--profile", "{profile}", "--seconds", "{seconds}", "--telemetry", "{run_dir}/telemetry.jsonl",
                "--record-inputs", "{run_dir}/inputs.json", "--decider", "{decider_mode}",
                "--tuning", "{tuning_file}"]
            cwd = project
        settings = {"backend": "jev", "model": "jev-1.14.0", "budget_usd": .1,
                    "call_budget_usd": .001, "max_calls": 1000,
                    "endpoint": f"http://127.0.0.1:{self.server.server_port}/v1/systemone",
                    "api_key_env": KEY_ENV, "timeout_s": .3, "max_elapsed_s": 1.,
                    "backoff_s": 0, "retries": 0, **overrides}
        path = project / "shakedown.toml"
        path.write_text("[game]\n" + f"cwd = {json.dumps(str(cwd))}\n"
            + f"command = {json.dumps(command)}\nmaps = [\"yard\"]\nprofiles = [\"cautious\"]\n"
            + "[batch]\n" + f"seeds = {json.dumps(list(seeds))}\ndeciders = [\"jev\"]\n"
            + f"seconds = {duration}\nworkers = {workers}\nwall_timeout_s = 5\nbootstrap_samples = 10\n"
            + "[decider]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items()),
            encoding="utf-8")
        return load_config(path)

    @staticmethod
    def read(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def assert_no_secret(self):
        encoded = SECRET.encode()
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(encoded, path.read_bytes(), str(path))

    def test_real_http_bridge_batch_cost_cache_and_game_environment(self):
        config = self.config()
        runtime = hosted_runtime(config, self.root / "shared-ledger.json", 2)
        first = self.root / "first"
        result = run_batch(config, first, hosted=runtime)
        self.assertEqual((result["total_runs"], result["valid_runs"], result["invalid_runs"]), (2, 2, 0))
        self.assertEqual(len(self.server.requests), 4)
        self.assertTrue(all(request["authorization"] == "Bearer " + SECRET for request in self.server.requests))
        self.assertIsNone(result["hosted"]["cost_usd"])
        self.assertEqual(result["hosted"]["http_attempts"], 4)
        self.assertEqual(result["hosted"]["cache_hits"], 0)
        self.assertAlmostEqual(result["hosted"]["estimated_cost_usd"], 4 * 32 * .042 / 1000000)
        for folder in sorted((first / "runs").iterdir()):
            summary = self.read(folder / "bridge_summary.json")
            self.assertTrue(summary["valid"])
            self.assertIsNone(summary["cost_usd"])
            self.assertEqual(summary["sources"], {"jev": 2})
            self.assertEqual(summary["hosted"]["model"], "jev-1.14.0")
            self.assertGreaterEqual(summary["latency_ms_p95"], summary["latency_ms_p50"])
            tape = [json.loads(line) for line in (folder / "decision_tape.jsonl").read_text().splitlines()]
            self.assertEqual({row["source"] for row in tape}, {"jev"})
            self.assertEqual({row["model"] for row in tape}, {"jev-1.14.0"})
            self.assertEqual(self.read(folder / "work/environment-check.json"),
                             {"custom_key_present": False, "default_key_present": False})
        for group in self.read(first / "metrics.json")["groups"]:
            self.assertEqual(group["metrics"]["cost_usd"], {"mean": None, "ci95": None, "n": 0})
        second = self.root / "second"
        cached = run_batch(config, second, hosted=runtime)
        self.assertEqual(cached["valid_runs"], 2)
        self.assertEqual(len(self.server.requests), 4)
        self.assertEqual(cached["hosted"]["cache_hits"], 4)
        for folder in (second / "runs").iterdir():
            self.assertEqual(self.read(folder / "bridge_summary.json")["sources"], {"cache": 2})
        self.assert_no_secret()

    def test_tinyraid_uses_hosted_bridge_with_local_provider_only(self):
        config = self.config(game="tinyraid", duration=.6)
        result = run_batch(config, self.root / "tinyraid")
        self.assertEqual((result["total_runs"], result["valid_runs"], result["invalid_runs"]), (2, 2, 0))
        self.assertGreater(len(self.server.requests), 0)
        self.assertIsNone(result["hosted"]["cost_usd"])
        self.assertEqual({row["decider"] for row in result["runs"]}, {"jev"})
        self.assert_no_secret()

    def test_pinned_model_drift_and_fatal_http_invalidate_batch(self):
        for mode in ("drift", "fatal"):
            with self.subTest(mode=mode):
                self.server.mode = mode
                config = self.config(seeds=(1,), workers=1, retries=2)
                path = self.root / mode
                result = run_batch(config, path)
                self.assertEqual((result["valid_runs"], result["invalid_runs"]), (0, 1))
                summary = self.read(path / "runs/r000001/bridge_summary.json")
                self.assertFalse(summary["valid"])
                self.assertIn("hosted_error", summary["invalid_reasons"])
                self.assertEqual(summary["hosted"]["http_attempts"], 1)
                self.assertEqual(self.read(path / "metrics.json")["valid_runs"], 0)
        self.assert_no_secret()

    def test_excess_fallback_invalidates_direct_case_and_batch(self):
        self.server.mode = "fallback"
        config = self.config(seeds=(1,), workers=1)
        direct = run_case(config, self.root / "direct-fallback", seed=1, map_name="yard",
                          profile="cautious", decider="jev")
        self.assertFalse(direct["valid"])
        self.assertIn("fallback_rate_over_10pct", direct["invalid_reasons"])
        result = run_batch(config, self.root / "fallback-batch")
        self.assertEqual((result["total_runs"], result["valid_runs"], result["invalid_runs"]), (1, 0, 1))
        summary = self.read(self.root / "fallback-batch/runs/r000001/bridge_summary.json")
        self.assertEqual(summary["fallback_rate"], 1)
        self.assertEqual(summary["sources"], {"fallback": 2})
        self.assertIsNone(summary["cost_usd"])
        self.assertIsNone(summary["estimated_cost_usd"])

    def test_exhausted_shared_budget_retains_denominator_without_second_child(self):
        config = self.config(budget_usd=.001, workers=1)
        with patch.object(bridge, "start_process", wraps=bridge.start_process) as start:
            result = run_batch(config, self.root / "exhausted")
        self.assertEqual(start.call_count, 1)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual((result["total_runs"], len(result["runs"])), (2, 2))
        self.assertEqual(result["invalid_runs"], 2)
        second = self.root / "exhausted/runs/r000002"
        self.assertFalse((second / "stdout.log").exists())
        self.assertFalse((second / "work/environment-check.json").exists())
        self.assertIn("budget", self.read(second / "run.json")["error"])
        self.assertEqual(result["hosted"]["http_attempts"], 1)
        self.assertEqual(len(self.read(self.root / "exhausted/metrics.json")["invalid_runs"]), 2)

    def test_unpinned_model_and_zero_budget_fail_before_network_or_output(self):
        for settings in ({"model": "jev-latest"}, {"budget_usd": 0}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.config(**settings)
        self.assertEqual(self.server.requests, [])
        self.assertFalse((self.root / "runs").exists())

    def test_approved_campaign_shares_hosted_runtime_across_profiles(self):
        config = self.config(seeds=(1,), workers=2)
        document = {"rounds": 1, "batch_size": 1, "search_ranges": {},
                    "profile_instructions": {"cautious": "Hold safely", "looter": "Loot carefully"},
                    "experiments": [{"id": "baseline", "profiles": ["cautious", "looter"],
                        "maps": ["yard"], "seeds": [1], "tuning_ranges": {}}]}
        targets = self.root / "targets.json"
        targets.write_text("{}", encoding="utf-8")
        def completed(_config, _path, **_kwargs):
            return {"status": "completed", "valid_runs": 1, "invalid_runs": 0, "runs": []}
        with patch("shakedown.planner.engine.approved_campaign", return_value=document), \
             patch("shakedown.planner.campaign.run_batch", side_effect=completed) as batch:
            result = run_approved_campaign(config, self.root / "campaign.json", self.root / "approval.json",
                self.root / "campaign", approved=True, targets=targets, targets_approval=self.root / "targets-approval.json")
        self.assertEqual((result["status"], result["total_runs"]), ("completed", 2))
        self.assertEqual(batch.call_count, 2)
        first, second = batch.call_args_list
        self.assertIs(first.kwargs["hosted"], second.kwargs["hosted"])
        self.assertEqual([call.args[0].instructions for call in batch.call_args_list],
                         ["Hold safely", "Loot carefully"])
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
