"""All planner documents through real offline Claude/Codex subprocess transports."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

from tests import cleanup_temp_directory
from shakedown.config import load_config
from shakedown.planner import engine
from shakedown.planner.mock import generate
from shakedown.planner.schema import STEPS, to_wire, validate_output


FIXTURE = Path(__file__).with_name("planner_schema_fixture.py").resolve()


class PlannerCLITransportTests(unittest.TestCase):
    def test_all_steps_cross_both_cli_dialects_and_share_budget(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, temporary)
        root = Path(temporary.name)
        example = Path(__file__).resolve().parents[1] / "examples/tinyraid/shakedown.toml"
        config = load_config(example)
        design = root / "design.md"
        design.write_text("Measure configured profiles. 관측 자료만 사용한다.", encoding="utf-8")
        metrics = root / "metrics.json"
        metrics.write_text(json.dumps({"groups": [{
            "profile": config.profiles[0], "map": config.maps[0],
            "metrics": {"extraction_rate": {"mean": .5, "ci95": [.2, .8], "n": 4}}
        }]}), encoding="utf-8")
        run = root / "run.json"
        run_value = {"command": [sys.executable, "-m", "examples.tinyraid", "--seed", "1"],
                     "cwd": str(example.parents[2])}
        run.write_text(json.dumps(run_value), encoding="utf-8")
        ledger = root / "budget.json"
        receipts = []
        for dialect in ("claude", "codex"):
            targets = approval = None
            for step in STEPS:
                with self.subTest(dialect=dialect, step=step):
                    inputs = ({"design_notes": design} if step in {"targets", "campaign"}
                              else {"run": run} if step == "bug" else {"metrics": metrics})
                    context = engine.context(config, inputs)
                    if step == "campaign":
                        context["approved_targets"] = json.loads(targets.read_text(encoding="utf-8"))
                    canonical = generate(step, context)
                    wire = to_wire(step, canonical)
                    wire_path = root / f"{dialect}-{step}-wire.json"
                    wire_path.write_text(json.dumps(wire, ensure_ascii=False), encoding="utf-8")
                    planner = {"command": [sys.executable, str(FIXTURE), "--dialect", dialect,
                                            "--output-json", str(wire_path)],
                               "codex_mode": "restricted", "timeout_s": 10}
                    configured = replace(config, raw={**config.raw, "planner": planner})
                    output = root / f"{dialect}-{step}"
                    receipt = engine.run_step(
                        configured, step, output, inputs=inputs, backend=f"{dialect}-cli",
                        targets=targets, targets_approval=approval, budget_usd=2,
                        call_budget_usd=.1, max_calls=20, ledger_path=ledger)
                    receipts.append(receipt)
                    self.assertEqual(receipt["status"], "passed")
                    self.assertEqual(len(receipt["attempts"]), 1)
                    attempt = receipt["attempts"][0]
                    self.assertEqual(attempt["status"], "passed")
                    self.assertEqual(attempt["usage"], {"input_tokens": 10, "output_tokens": 3})
                    self.assertEqual(attempt["metadata"]["exit_code"], 0)
                    self.assertEqual(attempt["metadata"]["tool_calls"], [])
                    artifact = Path(receipt["artifact"])
                    actual = json.loads(artifact.read_text(encoding="utf-8"))
                    validate_output(step, actual)
                    self.assertEqual(actual, canonical)
                    self.assertTrue((output / "attempt-1" / "stdout.log").is_file())
                    if dialect == "codex":
                        final = output / "attempt-1" / "last-message.json"
                        self.assertEqual(json.loads(final.read_text(encoding="utf-8")), wire)
                    if step == "targets":
                        targets = artifact
                        approval = root / f"{dialect}-targets-approved.json"
                        engine.approve_plan(configured, "targets", targets, approval, approved=True)
                    elif step == "report":
                        self.assertEqual(actual["results"][0]["value"], .5)
                        self.assertTrue((output / "report.md").is_file())
        self.assertEqual(len(receipts), 10)
        budget = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(len(budget["attempts"]), 10)
        self.assertTrue(all(attempt["settled"] for attempt in budget["attempts"]))
        self.assertEqual(sum(Decimal(attempt["charged_usd"]) for attempt in budget["attempts"]),
                         Decimal("0.65"))


if __name__ == "__main__":
    unittest.main()
