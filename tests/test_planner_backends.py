import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from shakedown.planner.backends import BackendError, MAX_BYTES, _command, call_backend


FIXTURE = Path(__file__).with_name("planner_cli_fixture.py").resolve()


class PlannerBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="planner transport ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index = 0

    def call(self, backend="claude", case="success", **kwargs):
        self.index += 1
        directory = self.root / f"attempt {self.index}"
        capture = self.root / f"capture {self.index}.json"
        self.directory, self.capture = directory, capture
        arguments = dict(prompt="한글 prompt $(never execute)\nnext", schema={"type": "object"},
            work_dir=directory, model="configured-alias", budget_usd=0.2,
            timeout_s=5, command=[sys.executable, str(FIXTURE), "--fixture-backend", backend,
                "--case", case, "--capture", str(capture)])
        if backend == "codex":
            arguments["allow_restricted_codex"] = True
        arguments.update(kwargs)
        return call_backend(backend + "-cli", **arguments)

    def test_claude_protocol_empty_cwd_and_literal_stdin(self):
        result = self.call()
        self.assertEqual(result.output, {"answer": "ok"})
        self.assertEqual(result.reported_cost_usd, 0.03)
        self.assertEqual(result.model, "observed-model")
        self.assertEqual(result.usage, {"input_tokens": 10, "output_tokens": 3})
        capture = json.loads(self.capture.read_text(encoding="utf-8"))
        argv = capture["argv"]
        self.assertEqual(capture["files"], [])
        self.assertTrue(Path(capture["cwd"]).samefile(self.directory / "empty-cwd"))
        self.assertIn("$(never execute)", capture["prompt"])
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), {"type": "object"})
        self.assertEqual(json.loads(argv[argv.index("--mcp-config") + 1]), {"mcpServers": {}})
        self.assertIn("--restricted", argv)
        self.assertIn("--safe-mode", argv)
        self.assertEqual(result.metadata["cost_enforcement"], "provider_estimate")
        self.assertFalse((self.directory / "transport-output.tmp").exists())
        self.assertTrue((self.directory / "transport.json").is_file())

    def test_claude_missing_output_is_for_engine_validation(self):
        self.assertIsNone(self.call(case="missing").output)

    def test_claude_unknown_cost_stays_unknown(self):
        self.assertIsNone(self.call(case="unknown_cost").reported_cost_usd)

    def test_claude_failures_and_cost_evidence(self):
        for case, cost, retryable in [("error", .03, False), ("nonzero", .03, False),
                                       ("zero_error", None, False), ("nan", None, False),
                                       ("malformed", None, True)]:
            with self.subTest(case=case), self.assertRaises(BackendError) as caught:
                self.call(case=case)
            self.assertEqual(caught.exception.reported_cost_usd, cost)
            self.assertEqual(caught.exception.retryable, retryable)

    def test_codex_requires_acknowledgement_before_artifacts(self):
        with self.assertRaisesRegex(BackendError, "tool-free"):
            self.call("codex", allow_restricted_codex=False)
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.directory.exists())

    def test_codex_protocol_restrictions_and_unknown_cost(self):
        result = self.call("codex")
        self.assertEqual(result.output, {"answer": "ok"})
        self.assertIsNone(result.reported_cost_usd)
        self.assertIsNone(result.model)
        self.assertEqual(result.usage["cached_input_tokens"], 2)
        capture = json.loads(self.capture.read_text(encoding="utf-8"))
        argv = capture["argv"]
        self.assertEqual(argv[0], "exec")
        self.assertEqual(argv[-1], "-")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn("skills.include_instructions=false", argv)
        self.assertNotIn("private-resume-id", json.dumps(result.metadata))
        self.assertNotIn("private-resume-id", (self.directory / "stdout.log").read_text(encoding="utf-8"))

    def test_codex_rejects_tools_failures_and_invalid_output(self):
        for case in ["tool", "unknown_event", "failed", "nonzero", "missing", "malformed"]:
            with self.subTest(case=case), self.assertRaises(BackendError) as caught:
                self.call("codex", case)
            self.assertIsNone(caught.exception.reported_cost_usd)
            self.assertEqual(caught.exception.retryable, case in {"missing", "malformed"})
            if case == "tool":
                self.assertEqual(caught.exception.metadata["tool_calls"], [{"type": "mcp_tool_call"}])
                self.assertNotIn("private-tool-id", json.dumps(caught.exception.metadata))

    def test_timeout_keeps_attempt_receipt_and_unknown_cost(self):
        with self.assertRaisesRegex(BackendError, "timed out") as caught:
            self.call(case="timeout", timeout_s=.2)
        self.assertTrue(caught.exception.metadata["wall_timeout"])
        self.assertIsNone(caught.exception.reported_cost_usd)
        self.assertTrue((self.directory / "transport.json").exists())
        self.assertFalse((self.directory / "transport-output.tmp").exists())

    def test_transient_windows_tmp_lock_still_removes_raw_and_keeps_receipt(self):
        denied = PermissionError(errno.EACCES, "sharing violation")
        original_unlink = Path.unlink
        denied_paths = []
        def unlinking(path):
            if path.name == "transport-output.tmp" and not denied_paths:
                denied_paths.append(path)
                raise denied
            return original_unlink(path)
        with patch("shakedown.planner.backends.os.name", "nt"), \
                patch("shakedown.planner.backends.Path.unlink", autospec=True, side_effect=unlinking), \
                patch("shakedown.planner.backends.time.sleep") as sleep:
            with self.assertRaisesRegex(BackendError, "timed out"):
                self.call(case="timeout", timeout_s=.2)
        self.assertEqual(len(denied_paths), 1)
        sleep.assert_called_with(.01)
        self.assertFalse((self.directory / "transport-output.tmp").exists())
        self.assertTrue((self.directory / "transport.json").exists())

    def test_secret_is_redacted_in_retained_evidence(self):
        secret = "fixture-secret-key-123456789"
        with patch.dict(os.environ, {"FIXTURE_API_KEY": secret}):
            result = self.call(case="secret")
        self.assertEqual(result.output["answer"], "[REDACTED]")
        for filename in ["stdout.log", "transport.json"]:
            self.assertNotIn(secret, (self.directory / filename).read_text(encoding="utf-8"))

    def test_input_and_output_limits(self):
        with self.assertRaisesRegex(BackendError, "input limit"):
            self.call(prompt="x" * (MAX_BYTES + 1))
        self.assertFalse(self.capture.exists())
        with self.assertRaisesRegex(BackendError, "output limit"):
            self.call(case="large")
        self.assertLess((self.directory / "stdout.log").stat().st_size, MAX_BYTES + 100)

    def test_existing_attempt_evidence_is_not_overwritten(self):
        self.call()
        receipt = (self.directory / "transport.json").read_bytes()
        with self.assertRaisesRegex(BackendError, "already exists"):
            call_backend("claude-cli", prompt="x", schema={}, work_dir=self.directory,
                model=None, budget_usd=.2, command=[sys.executable])
        self.assertEqual(receipt, (self.directory / "transport.json").read_bytes())

    def test_npm_wrapper_resolves_to_node_without_shell(self):
        wrapper = self.root / "codex.cmd"
        script = self.root / "node_modules/@openai/codex/bin/codex.js"
        script.parent.mkdir(parents=True)
        script.touch()
        with patch("shakedown.planner.backends.shutil.which", side_effect=[str(wrapper), "node.exe"]):
            self.assertEqual(_command("codex-cli", None), ["node.exe", str(script.resolve())])
        with self.assertRaisesRegex(BackendError, "shell wrapper"):
            _command("codex-cli", [str(wrapper)])


if __name__ == "__main__":
    unittest.main()
