"""Jev contract tests use only a loopback HTTP server, never a provider."""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import tests
from shakedown.deciders import DeciderError
from shakedown.deciders.jev import JevRuntime
from shakedown.planner.budget import BudgetError
from tests.test_deciders import request


def response(model="jev-1.14.0"):
    return {"model": model, "answers": {"tactic": {"type": "choice", "choice": "hold",
        "confidence": .8, "probabilities": {"hold": .8, "advance": .1, "reload": .1}}},
        "usage": {"input_tokens": 1000, "output_tokens": 10}}


class Fixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.responses = []
        self.requests = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                with fixture.lock:
                    fixture.requests.append({"body": json.loads(raw), "auth": self.headers.get("Authorization")})
                    selected = fixture.responses.pop(0) if fixture.responses else {}
                if selected.get("delay"):
                    time.sleep(selected["delay"])
                self.send_response(selected.get("status", 200))
                for key, value in selected.get("headers", {}).items():
                    self.send_header(key, value)
                self.end_headers()
                body = selected.get("raw")
                if body is None:
                    body = json.dumps(selected.get("body", response())).encode("utf-8")
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join()


class JevTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jev fixture ")
        self.addCleanup(tests.cleanup_temp_directory, self.temp)
        self.root = Path(self.temp.name)
        self.server = Fixture()
        self.addCleanup(self.server.close)
        self.environment = patch.dict(os.environ, {"JEV_TEST_KEY": "fixture-secret-not-for-logs"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.count = 0

    def runtime(self, **overrides):
        self.count += 1
        settings = {"model": "jev-1.14.0", "endpoint": f"http://127.0.0.1:{self.server.server_port}/v1/systemone",
                    "api_key_env": "JEV_TEST_KEY", "budget_usd": .1,
                    "call_budget_usd": .001, "backoff_s": 0, "max_elapsed_s": 1,
                    "timeout_s": .3, **overrides}
        return JevRuntime(settings, self.root / f"ledger-{self.count}.json", max_concurrency=4)

    def decider(self, runtime, name="run"):
        decider = runtime.new_decider(seed=7, run_id=name)
        self.addCleanup(decider.close)
        return decider

    def test_success_exact_contract_and_conservative_cost(self):
        runtime = self.runtime()
        decider = self.decider(runtime)
        result = decider.decide(request(), "Stay cautious")
        self.assertEqual((result.choice, result.source, result.attempts, result.model),
                         ("hold", "jev", 1, "jev-1.14.0"))
        sent = self.server.requests[0]
        self.assertEqual(sent["auth"], "Bearer fixture-secret-not-for-logs")
        self.assertEqual(sent["body"], {"model": "jev-1.14.0", "state": request()["state"],
            "questions": {"tactic": {"type": "choice", "instructions": "Stay cautious",
                                      "criteria": request()["candidates"]}}})
        summary = decider.summary()
        self.assertIsNone(summary["cost_usd"])
        self.assertAlmostEqual(summary["estimated_cost_usd"], .000042)
        self.assertEqual(summary["usage_tokens"], {"input_tokens": 1000, "output_tokens": 10})
        self.assertEqual(summary["budget"]["attempts"][0]["cost_status"], "unknown")
        self.assertEqual(summary["budget"]["charged_usd"], "0.001")
        self.assertNotIn("fixture-secret", json.dumps(summary))

    def test_cache_is_instruction_model_endpoint_and_request_specific(self):
        runtime = self.runtime(cache=self.root / "cache.sqlite")
        first = self.decider(runtime)
        self.assertEqual(first.decide(request(), "one").source, "jev")
        cached = first.decide(request(), "one")
        self.assertEqual((cached.source, cached.attempts), ("cache", 0))
        self.assertEqual(first.decide(request(), "two").source, "jev")
        self.assertEqual(first.decide(request(2), "one").source, "jev")
        self.assertEqual(len(self.server.requests), 3)
        other = self.runtime(cache=self.root / "cache.sqlite")
        second = self.decider(other)
        self.assertEqual(second.decide(request(), "one").source, "cache")
        second.close()
        summary = second.summary()
        self.assertEqual(summary["budget"]["charged_usd"], "0")
        self.assertEqual(summary["cost_usd"], 0)
        self.server.responses.append({"body": response("jev-1.13.0")})
        third = self.decider(self.runtime(cache=self.root / "cache.sqlite", model="jev-1.13.0"))
        self.assertEqual(third.decide(request(), "one").source, "jev")
        endpoint = f"http://127.0.0.1:{self.server.server_port}/another"
        fourth = self.decider(self.runtime(cache=self.root / "cache.sqlite", endpoint=endpoint))
        self.assertEqual(fourth.decide(request(), "one").source, "jev")
        self.assertNotIn(b"fixture-secret", (self.root / "cache.sqlite").read_bytes())

    def test_preadmission_exhaustion_and_unused_reservation(self):
        runtime = self.runtime(budget_usd=.001)
        first = self.decider(runtime)
        with self.assertRaises(BudgetError):
            runtime.new_decider(run_id="blocked")
        self.assertEqual(self.server.requests, [])
        first.close()
        self.assertEqual(runtime.ledger.remaining(), "0.001")
        second = self.decider(runtime, "next")
        second.decide(request(), "")
        with self.assertRaises(BudgetError):
            runtime.new_decider(run_id="spent")
        self.assertEqual(len(self.server.requests), 1)

    def test_rate_limit_halves_shared_cap_and_retries(self):
        self.server.responses += [{"status": 429, "headers": {"retry-after-ms": "1"}}, {}]
        runtime = self.runtime()
        result = self.decider(runtime).decide(request(), "")
        self.assertEqual((result.source, result.attempts), ("jev", 2))
        summary = runtime.summary()
        self.assertEqual(summary["cap"], 2)
        self.assertEqual(summary["cap_events"], [{"status": 429, "previous_cap": 4, "cap": 2}])
        self.assertFalse(summary["usage_complete"])
        self.assertIsNone(summary["estimated_cost_usd"])
        self.assertEqual(len(summary["budget"]["attempts"]), 2)

    def test_server_errors_fall_back_after_two_retries_with_total_latency(self):
        self.server.responses += [{"status": 529}, {"status": 503}, {"status": 500}]
        runtime = self.runtime(backoff_s=.01)
        result = self.decider(runtime).decide(request(), "")
        self.assertEqual((result.source, result.attempts), ("fallback", 3))
        self.assertIsNone(result.model)
        self.assertEqual(runtime.summary()["models"], [])
        self.assertEqual(runtime.summary()["requested_model"], "jev-1.14.0")
        self.assertIn(result.choice, request()["candidates"])
        self.assertGreaterEqual(result.latency_ms, 29)
        self.assertEqual(len(runtime.summary()["budget"]["attempts"]), 3)

    def test_timeout_retries_are_bounded_and_fall_back(self):
        self.server.responses += [{"delay": .1}, {"delay": .1}, {"delay": .1}]
        runtime = self.runtime(timeout_s=.015, max_elapsed_s=.2)
        before = time.monotonic()
        result = self.decider(runtime).decide(request(), "")
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.attempts, 3)
        self.assertLess(time.monotonic() - before, .2)
        self.assertIsNone(runtime.summary()["cost_usd"])

    def test_retry_after_cannot_extend_total_deadline(self):
        self.server.responses.append({"status": 429, "headers": {"Retry-After": "60"}})
        runtime = self.runtime(max_elapsed_s=.1)
        result = self.decider(runtime).decide(request(), "")
        self.assertEqual((result.source, result.attempts), ("fallback", 1))
        self.assertLess(result.latency_ms, 100)

    def test_permanent_http_errors_do_not_retry_or_leak_response(self):
        for status in (400, 401, 402, 413, 415, 422):
            with self.subTest(status=status):
                self.server.responses.append({"status": status, "raw": b"fixture-secret-not-for-logs"})
                runtime = self.runtime()
                decider = self.decider(runtime)
                with self.assertRaisesRegex(DeciderError, f"HTTP {status}") as captured:
                    decider.decide(request(), "")
                self.assertNotIn("fixture-secret", str(captured.exception))
                self.assertEqual(decider.summary()["http_attempts"], 1)
                self.assertEqual(len(decider.summary()["budget"]["attempts"]), 1)

    def test_model_drift_and_invalid_output_are_fatal(self):
        invalid = []
        invalid.append(response("jev-1.13.0"))
        value = response()
        value["answers"]["tactic"]["probabilities"]["hold"] = float("nan")
        invalid.append(value)
        value = response()
        value["answers"]["tactic"]["choice"] = "advance"
        invalid.append(value)
        value = response()
        del value["answers"]["tactic"]["probabilities"]["reload"]
        invalid.append(value)
        for envelope in invalid:
            with self.subTest(envelope=envelope):
                self.server.responses.append({"body": envelope})
                decider = self.decider(self.runtime())
                with self.assertRaises(DeciderError):
                    decider.decide(request(), "")
                self.assertEqual(decider.summary()["http_attempts"], 1)
                self.assertTrue(decider.summary()["invalid_reasons"])

    def test_missing_usage_does_not_claim_zero_cost(self):
        value = response()
        del value["usage"]
        self.server.responses.append({"body": value})
        decider = self.decider(self.runtime())
        self.assertEqual(decider.decide(request(), "").source, "jev")
        summary = decider.summary()
        self.assertIsNone(summary["estimated_cost_usd"])
        self.assertIsNone(summary["cost_usd"])
        self.assertFalse(summary["usage_complete"])

    def test_retry_budget_failure_stops_without_fallback_or_second_http(self):
        self.server.responses.append({"status": 503})
        runtime = self.runtime(budget_usd=.001)
        decider = self.decider(runtime)
        with self.assertRaisesRegex(DeciderError, "budget admission is unavailable or exhausted"):
            decider.decide(request(), "")
        self.assertEqual(decider.summary()["http_attempts"], 1)
        self.assertEqual(len(self.server.requests), 1)

    def test_retry_ledger_permission_error_is_fatal_without_network_fallback(self):
        self.server.responses.append({"status": 503})
        runtime = self.runtime()
        decider = self.decider(runtime)
        diagnostic = "Jev budget admission is unavailable or exhausted"
        with patch.object(runtime, "_reserve", side_effect=PermissionError("private ledger path")) as reserve:
            with patch.object(decider._fallback, "decide") as fallback:
                with self.assertRaisesRegex(DeciderError, diagnostic) as captured:
                    decider.decide(request(), "")
        reserve.assert_called_once_with()
        fallback.assert_not_called()
        self.assertNotIn("private ledger path", str(captured.exception))
        summary = decider.summary()
        self.assertIn(diagnostic, summary["invalid_reasons"])
        self.assertEqual(summary["http_attempts"], 1)
        self.assertEqual(len(summary["budget"]["attempts"]), 1)
        self.assertEqual(len(self.server.requests), 1)

    def test_malformed_and_oversized_response_fail_without_retry(self):
        for body in (b"{bad", b"x" * (1024 * 1024 + 1)):
            with self.subTest(size=len(body)):
                self.server.responses.append({"raw": body})
                decider = self.decider(self.runtime())
                with self.assertRaises(DeciderError):
                    decider.decide(request(), "")
                self.assertEqual(decider.summary()["http_attempts"], 1)
                self.assertIsNone(decider.summary()["estimated_cost_usd"])

    def test_tied_argmax_is_stable_and_unknown_usage_is_bounded(self):
        value = response()
        value["answers"]["tactic"].update(choice="hold", probabilities={"hold": .5, "advance": .5, "reload": 0})
        value["usage"]["input_tokens"] = 10 ** 1000
        self.server.responses.append({"body": value})
        decider = self.decider(self.runtime())
        self.assertEqual(decider.decide(request(), "").choice, "advance")
        self.assertFalse(decider.summary()["usage_complete"])
        self.assertIsNone(decider.summary()["estimated_cost_usd"])

    def test_redirect_is_not_followed(self):
        self.server.responses.append({"status": 302, "headers": {
            "Location": f"http://127.0.0.1:{self.server.server_port}/redirect"}})
        decider = self.decider(self.runtime())
        with self.assertRaisesRegex(DeciderError, "HTTP 302"):
            decider.decide(request(), "")
        self.assertEqual(len(self.server.requests), 1)

    def test_settings_and_key_preflight_before_ledger(self):
        for settings in ({"model": "jev-latest"}, {"endpoint": "http://example.com/v1/systemone"},
                         {"endpoint": "https://name:password@example.com/v1/systemone"},
                         {"retries": 3}, {"timeout_s": float("nan")}, {"budget_usd": 0}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.runtime(**settings)
        with patch.dict(os.environ, {"JEV_TEST_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "API key"):
                self.runtime()
        self.assertEqual(list(self.root.glob("ledger-*.json")), [])

    def test_shared_runtime_is_thread_safe(self):
        runtime = self.runtime(cache=self.root / "cache.sqlite")
        deciders = [self.decider(runtime, str(index)) for index in range(4)]
        results, errors = [], []
        def call(index):
            try:
                results.append(deciders[index].decide(request(index + 1), ""))
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=call, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(runtime.summary()["http_attempts"], 4)
        self.assertEqual(runtime.summary()["usage_tokens"]["input_tokens"], 4000)


if __name__ == "__main__":
    unittest.main()
