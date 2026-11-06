"""Public planner commands keep drafts, approvals, and execution separate."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401

from shakedown.cli import main


CONFIG = Path(__file__).resolve().parents[1] / "examples/tinyraid/shakedown.toml"


class PlannerCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.design = self.root / "design.md"
        self.design.write_text("Measure the configured baseline first.", encoding="utf-8")

    def command(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = main([str(value) for value in arguments])
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def success(self, *arguments):
        code, stdout, stderr = self.command(*arguments)
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def draft_targets(self):
        receipt = self.success("plan", "targets", "--config", CONFIG, "--backend", "mock",
                               "--input", f"design_notes={self.design}", "--output", self.root / "targets")
        return Path(receipt["artifact"])

    def approved_campaign(self):
        targets = self.draft_targets()
        targets_approval = self.root / "targets-approval.json"
        self.success("approve", "targets", "--config", CONFIG, "--artifact", targets,
                     "--receipt", targets_approval, "--approved")
        receipt = self.success("plan", "campaign", "--config", CONFIG, "--backend", "mock",
                               "--input", f"design_notes={self.design}", "--output", self.root / "campaign",
                               "--targets", targets, "--targets-approval", targets_approval)
        campaign = Path(receipt["artifact"])
        approval = self.root / "campaign-approval.json"
        self.success("approve", "campaign", "--config", CONFIG, "--artifact", campaign,
                     "--receipt", approval, "--approved", "--targets", targets,
                     "--targets-approval", targets_approval)
        return campaign, approval, targets, targets_approval

    def execution_args(self, paths):
        campaign, approval, targets, targets_approval = paths
        return ["campaign", "--config", CONFIG, "--campaign", campaign, "--approval", approval,
                "--targets", targets, "--targets-approval", targets_approval,
                "--output", self.root / "executed", "--max-runs", "100"]

    def test_full_mock_drafting_approval_and_execution_command_workflow(self):
        with patch("shakedown.planner.engine.call_backend") as backend:
            paths = self.approved_campaign()
        backend.assert_not_called()
        batch_result = {"status": "completed", "valid_runs": 20, "invalid_runs": 0, "runs": []}
        with patch("shakedown.planner.campaign.run_batch", return_value=batch_result) as batch:
            receipt = self.success(*self.execution_args(paths), "--approved")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["total_runs"], 60)
        self.assertEqual(batch.call_count, 3)
        self.assertEqual({call.args[0].profiles[0] for call in batch.call_args_list},
                         {"cautious", "aggressive", "looter"})
        stored = json.loads((self.root / "executed/campaign_run.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, receipt)

    def test_duplicate_and_malformed_input_arguments_fail_before_engine(self):
        for inputs in (["design_notes=a", "design_notes=b"], ["design_notes"], ["=a"], ["design_notes="]):
            with self.subTest(inputs=inputs):
                arguments = ["plan", "targets", "--config", CONFIG, "--backend", "mock",
                             "--output", self.root / "invalid"]
                for value in inputs:
                    arguments.extend(["--input", value])
                with patch("shakedown.planner.engine.run_step") as run:
                    code, _, stderr = self.command(*arguments)
                self.assertEqual(code, 2)
                self.assertIn("unique NAME=PATH", stderr)
                run.assert_not_called()
        self.assertFalse((self.root / "invalid").exists())

    def test_approve_command_requires_explicit_flag_and_draft_is_not_approval(self):
        targets = self.draft_targets()
        approval = self.root / "approval.json"
        code, _, stderr = self.command("approve", "targets", "--config", CONFIG,
                                        "--artifact", targets, "--receipt", approval)
        self.assertEqual(code, 2)
        self.assertIn("explicit approval", stderr)
        self.assertFalse(approval.exists())
        code, _, _ = self.command("plan", "campaign", "--config", CONFIG, "--backend", "mock",
                                  "--input", f"design_notes={self.design}", "--output", self.root / "unapproved",
                                  "--targets", targets)
        self.assertEqual(code, 2)

    def test_campaign_execution_without_flag_cannot_launch_game(self):
        paths = self.approved_campaign()
        with patch("shakedown.planner.campaign.run_batch") as batch:
            code, _, stderr = self.command(*self.execution_args(paths))
        self.assertEqual(code, 2)
        self.assertIn("explicit campaign execution approval", stderr)
        batch.assert_not_called()
        self.assertFalse((self.root / "executed").exists())


if __name__ == "__main__":
    unittest.main()
