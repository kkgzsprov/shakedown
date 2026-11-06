"""Offline planner orchestration, approval boundaries, and retry accounting."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401 - Make source imports work without installation.

from shakedown.config import load_config
from shakedown.planner import engine
from shakedown.planner.backends import BackendError, BackendResult
from shakedown.planner.mock import generate
from shakedown.planner.schema import to_wire, validate_output


class PlannerEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        example = Path(__file__).resolve().parents[1] / "examples/tinyraid/shakedown.toml"
        self.config = load_config(example)
        self.design = self.root / "design.md"
        self.design.write_text("Measure extraction by configured profile before proposing tuning.", encoding="utf-8")
        self.metrics = self.write_json("metrics.json", {"groups": []})
        self.run = self.write_json("run.json", {"command": ["python", "-m", "examples.tinyraid", "--seed", "1"],
                                               "cwd": str(example.parent.parent.parent)})
        self.inputs = {"design_notes": self.design}

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def draft_targets(self, name="targets"):
        receipt = engine.run_step(self.config, "targets", self.root / name,
                                  inputs=self.inputs, backend="mock")
        return Path(receipt["artifact"])

    def approved_targets(self):
        targets = self.draft_targets()
        approval = self.root / "targets-approval.json"
        engine.approve_plan(self.config, "targets", targets, approval, approved=True)
        return targets, approval

    def external_options(self):
        return {"backend": "claude-cli", "inputs": self.inputs, "budget_usd": .2,
                "call_budget_usd": .1, "max_calls": 2,
                "ledger_path": self.root / "budget.json"}

    def valid_backend_result(self, step="targets"):
        value = generate(step, engine.context(self.config, self.inputs))
        return BackendResult(to_wire(step, value), .05, {}, "fixture", {})

    def test_mock_targets_campaign_and_explicit_approval_workflow_is_offline(self):
        with patch.object(engine, "call_backend") as backend:
            targets, targets_approval = self.approved_targets()
            receipt = engine.run_step(self.config, "campaign", self.root / "campaign",
                                      inputs=self.inputs, backend="mock", targets=targets,
                                      targets_approval=targets_approval)
            campaign = Path(receipt["artifact"])
            approval = self.root / "campaign-approval.json"
            approved = engine.approve_plan(self.config, "campaign", campaign, approval,
                                           approved=True, targets=targets,
                                           targets_approval=targets_approval)
        backend.assert_not_called()
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(campaign.is_absolute())
        validate_output("campaign", json.loads(campaign.read_text(encoding="utf-8")))
        self.assertIs(approved["approved"], True)
        self.assertTrue(approval.is_file())
        self.assertFalse((self.root / "budget.json").exists())

    def test_draft_never_implicitly_approves_itself(self):
        targets = self.draft_targets()
        approval = self.root / "approval.json"
        with self.assertRaises(ValueError):
            engine.approve_plan(self.config, "targets", targets, approval)
        self.assertFalse(approval.exists())
        with self.assertRaises(ValueError):
            engine.run_step(self.config, "campaign", self.root / "campaign",
                            inputs=self.inputs, backend="mock", targets=targets)

    def test_targets_approval_is_bound_to_configuration_and_document_contents(self):
        targets, approval = self.approved_targets()
        altered = replace(self.config, profiles=("cautious",))
        with self.assertRaises(ValueError):
            engine.run_step(altered, "campaign", self.root / "changed-config",
                            inputs=self.inputs, backend="mock", targets=targets,
                            targets_approval=approval)
        value = json.loads(targets.read_text(encoding="utf-8"))
        value["notes"] += " Human changed this draft."
        targets.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            engine.run_step(self.config, "campaign", self.root / "changed-targets",
                            inputs=self.inputs, backend="mock", targets=targets,
                            targets_approval=approval)

    def test_campaign_approval_requires_current_target_approval(self):
        targets, targets_approval = self.approved_targets()
        receipt = engine.run_step(self.config, "campaign", self.root / "campaign",
                                  inputs=self.inputs, backend="mock", targets=targets,
                                  targets_approval=targets_approval)
        campaign = Path(receipt["artifact"])
        with self.assertRaises(ValueError):
            engine.approve_plan(self.config, "campaign", campaign, self.root / "no-targets.json", approved=True)
        content = json.loads(targets.read_text(encoding="utf-8"))
        content["notes"] += " changed"
        targets.write_text(json.dumps(content), encoding="utf-8")
        with self.assertRaises(ValueError):
            engine.approve_plan(self.config, "campaign", campaign, self.root / "stale-targets.json",
                                approved=True, targets=targets, targets_approval=targets_approval)

    def test_remaining_mock_steps_use_supplied_evidence(self):
        for step, inputs in (("revise", {"metrics": self.metrics}),
                             ("report", {"metrics": self.metrics}),
                             ("bug", {"run": self.run})):
            with self.subTest(step=step):
                receipt = engine.run_step(self.config, step, self.root / step, inputs=inputs, backend="mock")
                result = json.loads(Path(receipt["artifact"]).read_text(encoding="utf-8"))
                self.assertEqual(receipt["status"], "passed")
                validate_output(step, result)
                if step == "report":
                    self.assertEqual(result["results"], [])
                if step == "bug":
                    self.assertEqual(result["reproduction"], json.loads(self.run.read_text(encoding="utf-8")))

    def test_required_inputs_fail_before_external_execution(self):
        with patch.object(engine, "call_backend") as backend:
            for step in ("targets", "campaign", "revise", "report", "bug"):
                with self.subTest(step=step), self.assertRaises(ValueError):
                    engine.run_step(self.config, step, self.root / step, backend="claude-cli",
                                    budget_usd=.2, call_budget_usd=.1, max_calls=2)
        backend.assert_not_called()

    def test_malformed_measurements_fail_before_spending_or_output(self):
        for name, estimate in (
            ("metrics-array", []),
            ("invalid-n", {"extraction_rate": {"mean": .5, "ci95": [0., 1.], "n": -1}}),
            ("nonfinite-ci", {"extraction_rate": {"mean": .5, "ci95": [0., float("inf")], "n": 1}}),
        ):
            with self.subTest(name=name):
                metrics = self.write_json(name + ".json", {"groups": [
                    {"profile": "cautious", "map": "yard", "metrics": estimate}]})
                if name == "nonfinite-ci":
                    # Valid JSON numeric syntax may still overflow a float;
                    # exercise evidence validation beyond NaN token rejection.
                    metrics.write_text(metrics.read_text(encoding="utf-8").replace("Infinity", "1e999"),
                                       encoding="utf-8")
                output = self.root / name
                with patch.object(engine, "call_backend") as backend:
                    with self.assertRaises(ValueError):
                        engine.run_step(self.config, "report", output, inputs={"metrics": metrics},
                            backend="claude-cli", budget_usd=.2, call_budget_usd=.1,
                            ledger_path=self.root / (name + "-budget.json"))
                    backend.assert_not_called()
                self.assertFalse(output.exists())
                self.assertFalse((self.root / (name + "-budget.json")).exists())

    def test_schema_failure_retries_once_then_records_passed_artifact(self):
        valid = self.valid_backend_result()
        invalid = BackendResult({}, .05, {}, "fixture", {})
        with patch.object(engine, "call_backend", side_effect=[invalid, valid]) as backend:
            receipt = engine.run_step(self.config, "targets", self.root / "retry", **self.external_options())
        self.assertEqual(backend.call_count, 2)
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(len(receipt["attempts"]), 2)
        validate_output("targets", json.loads(Path(receipt["artifact"]).read_text(encoding="utf-8")))

    def test_repeated_schema_failure_persists_failed_receipt_and_charges(self):
        output = self.root / "invalid"
        invalid = BackendResult({}, None, {}, "fixture", {})
        with patch.object(engine, "call_backend", return_value=invalid) as backend:
            with self.assertRaises(ValueError):
                engine.run_step(self.config, "targets", output, **self.external_options())
        self.assertEqual(backend.call_count, 2)
        receipt = json.loads((output / "planner_run.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(len(receipt["attempts"]), 2)
        ledger = json.loads((self.root / "budget.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(Decimal(row["charged_usd"]) for row in ledger["attempts"]), Decimal(".2"))

    def test_runtime_failure_keeps_reservation_and_ledger_blocks_later_process(self):
        options = self.external_options()
        options.update(budget_usd=.1, max_calls=1)
        with patch.object(engine, "call_backend", side_effect=BackendError("fixture timeout")) as backend:
            with self.assertRaises(ValueError):
                engine.run_step(self.config, "targets", self.root / "timeout", **options)
            with self.assertRaises(ValueError):
                engine.run_step(self.config, "targets", self.root / "exhausted", **options)
        self.assertEqual(backend.call_count, 1)
        ledger = json.loads((self.root / "budget.json").read_text(encoding="utf-8"))
        self.assertEqual(len(ledger["attempts"]), 1)
        self.assertEqual(Decimal(ledger["attempts"][0]["charged_usd"]), Decimal(".1"))
        self.assertEqual(ledger["attempts"][0]["cost_status"], "unknown")
        for name in ("timeout", "exhausted"):
            self.assertEqual(json.loads((self.root / name / "planner_run.json").read_text(encoding="utf-8"))["status"], "failed")
        exhausted = json.loads((self.root / "exhausted/planner_run.json").read_text(encoding="utf-8"))
        self.assertEqual(exhausted["attempts"][0]["status"], "admission_denied")
        self.assertNotIn("reservation_id", exhausted["attempts"][0])
        self.assertNotIn("absent or already settled", exhausted["error"])
        self.assertEqual(exhausted["budget"]["remaining_usd"], "0")
        self.assertEqual(len(exhausted["budget"]["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
