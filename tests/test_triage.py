"""Triage counts signatures only after evidence-backed, independent replay."""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import tests  # noqa: F401

from shakedown.config import load_config
from shakedown.orchestrate import run_case, write_json
from shakedown.protocol import canonical_json
from shakedown.triage import finding_signature, triage_batch


ROOT = Path(__file__).resolve().parents[1]


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = replace(load_config(ROOT / "examples/tinyraid/shakedown.toml"), duration=14)

    def batch(self, name, *, seed=1, decider="rule", extra=("--scenario", "corner_pocket", "--bug", "corner_pocket")):
        folder = self.base / name
        (folder / "runs").mkdir(parents=True)
        record = run_case(self.config, folder / "runs/r1", seed=seed, map_name="yard",
                          profile="cautious", duration=self.config.duration, decider=decider,
                          extra_args=extra)
        self.assertEqual(record["extra_args"], list(extra))
        record["variant"] = "corner"
        write_json(folder / "runs/r1/run.json", record)
        write_json(folder / "batch.json", {"v": 1, "total_runs": 1,
                                           "planned_runs": [record], "runs": [record]})
        (folder / "findings.jsonl").write_bytes(canonical_json(record) + b"\n")
        return folder, record

    def test_signatures_normalize_digits_and_nearby_regions_but_keep_oracle_kind(self):
        meta = {"map": "yard", "valid": True}
        first = [{"type": "oracle", "oracle": "stuck", "detail": "agent 12 at cell 4", "x": 8, "y": 6},
                 {"type": "run_end", "outcome": "oracle", "oracle": "stuck", "x": 8, "y": 6}]
        second = [{"type": "oracle", "oracle": "stuck", "detail": "agent 99 at cell 8", "x": 10, "y": 7},
                  {"type": "run_end", "outcome": "oracle", "oracle": "stuck", "x": 10, "y": 7}]
        self.assertEqual(finding_signature(meta, first), finding_signature(meta, second))
        second[0]["oracle"] = second[-1]["oracle"] = "numeric"
        self.assertNotEqual(finding_signature(meta, first), finding_signature(meta, second))

    def test_actual_corner_oracle_and_independent_inputs_reproduce(self):
        batch, _ = self.batch("corner")
        report = triage_batch(self.config, batch, self.base / "catalog")
        self.assertEqual(report["reproduced"], 1, report)
        entry = report["entries"][0]
        self.assertEqual(entry["signature"]["oracle"], "stuck")
        self.assertTrue(entry["reproduction"]["policy_match"])
        self.assertTrue(entry["reproduction"]["input_match"])
        draft = json.loads((self.base / "catalog/findings" / entry["draft"]).read_text())
        self.assertIsNone(draft["suspected_cause"])
        self.assertEqual(draft["reproduction"]["status"], "reproduced")

    def test_same_batch_is_idempotent_and_new_batch_adds_one_source_without_retry(self):
        batch, _ = self.batch("first")
        output = self.base / "catalog"
        first = triage_batch(self.config, batch, output)
        with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("implicit retry")):
            second = triage_batch(self.config, batch, output)
            self.assertEqual(second["new_sources"], 0)
            self.assertEqual(second["entries"][0]["run_count"], 1)
        other, _ = self.batch("second", seed=7)
        with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("duplicate signature replay")):
            third = triage_batch(self.config, other, output)
        self.assertEqual(third["new_signatures"], 0)
        self.assertEqual(third["new_sources"], 1)
        self.assertEqual(third["entries"][0]["run_count"], 2)
        self.assertEqual(first["entries"][0]["id"], third["entries"][0]["id"])

    def test_bridge_oracle_uses_tape_then_independent_inputs(self):
        batch, _ = self.batch("mock", decider="mock")
        report = triage_batch(self.config, batch, self.base / "catalog")
        self.assertEqual(report["reproduced"], 1, report)
        entry = report["entries"][0]
        reproduced = json.loads((self.base / "catalog/findings" / entry["id"] / "reproduction/run.json").read_text())
        self.assertEqual(reproduced["decider"], "tape")

    def test_missing_source_or_missing_inputs_never_claims_reproduction(self):
        for missing in ("telemetry.jsonl", "inputs.json", "run.json"):
            with self.subTest(missing=missing):
                batch, _ = self.batch(missing.replace(".", "-"))
                (batch / "runs/r1" / missing).unlink()
                with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("missing proof")):
                    report = triage_batch(self.config, batch, self.base / ("catalog-" + missing))
                self.assertEqual(report["reproduced"], 0)
                self.assertEqual(report["unmeasured"], 1, report)

    def test_failed_input_replay_assertion_does_not_count_as_reproduced(self):
        batch, _ = self.batch("changed-input")
        inputs = batch / "runs/r1/inputs.json"
        recording = json.loads(inputs.read_text())
        recording["result"]["events_hash"] = "0" * 64
        write_json(inputs, recording)
        report = triage_batch(self.config, batch, self.base / "catalog")
        self.assertEqual(report["unreproduced"], 1, report)
        self.assertFalse(report["entries"][0]["reproduction"]["input_match"])

    def test_wrong_configuration_hash_blocks_reproduction(self):
        batch, record = self.batch("changed-config")
        record["config_hash"] = "0" * 64
        write_json(batch / "runs/r1/run.json", record)
        (batch / "findings.jsonl").write_bytes(canonical_json(record) + b"\n")
        with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("untrusted replay")):
            report = triage_batch(self.config, batch, self.base / "catalog")
        self.assertEqual(report["unmeasured"], 1)

    def test_changed_runtime_overrides_do_not_reproduce_a_different_fixture(self):
        for rewrite_inventory in (False, True):
            with self.subTest(rewrite_inventory=rewrite_inventory):
                name = "changed-args-all" if rewrite_inventory else "changed-args-run"
                batch, record = self.batch(name)
                record["extra_args"] = ["--scenario", "stuck"]
                write_json(batch / "runs/r1/run.json", record)
                if rewrite_inventory:
                    write_json(batch / "batch.json", {"v": 1, "total_runs": 1,
                                                       "planned_runs": [record], "runs": [record]})
                    (batch / "findings.jsonl").write_bytes(canonical_json(record) + b"\n")
                with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("different fixture launched")):
                    report = triage_batch(self.config, batch, self.base / (name + "-catalog"))
                self.assertEqual(report["reproduced"], 0, report)
                self.assertEqual(report["unmeasured"], 1, report)
                self.assertFalse(report["entries"][0]["sources"][0]["validated"])

    def test_changed_cwd_or_duration_cannot_bypass_executed_command_provenance(self):
        for field, value in (("cwd", str(self.base)), ("seconds", 20)):
            with self.subTest(field=field):
                batch, record = self.batch("changed-" + field)
                record[field] = value
                write_json(batch / "runs/r1/run.json", record)
                write_json(batch / "batch.json", {"v": 1, "total_runs": 1,
                                                   "planned_runs": [record], "runs": [record]})
                (batch / "findings.jsonl").write_bytes(canonical_json(record) + b"\n")
                with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("different replay launched")):
                    report = triage_batch(self.config, batch, self.base / (field + "-catalog"))
                self.assertEqual(report["unmeasured"], 1, report)

    def test_anomaly_requires_underlying_condition_not_matching_timeout_alone(self):
        batch, _ = self.batch("quiet", extra=(), seed=1)
        (batch / "findings.jsonl").write_bytes(b"")
        write_json(batch / "anomalies.json", {"findings": [{"mechanism": "anomaly",
                   "signal": "extraction_zone_timeout", "group": {"map": "yard", "profile": "cautious", "variant": "corner"},
                   "run_ids": ["r1"]}]})
        with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("unsupported anomaly replay")):
            report = triage_batch(self.config, batch, self.base / "catalog")
        self.assertEqual(report["reproduced"], 0)
        self.assertEqual(report["unmeasured"], 1)

    def test_actual_unwarned_explosion_anomaly_reproduces_its_condition_and_inputs(self):
        batch, _ = self.batch("silent", extra=("--scenario", "grenade", "--bug", "silent_grenade"))
        (batch / "findings.jsonl").write_bytes(b"")
        write_json(batch / "anomalies.json", {"findings": [{"mechanism": "anomaly",
                   "signal": "unwarned_explosion_death",
                   "group": {"map": "yard", "profile": "cautious", "variant": "corner"},
                   "run_ids": ["r1"]}]})
        report = triage_batch(self.config, batch, self.base / "catalog")
        self.assertEqual(report["reproduced"], 1, report)
        self.assertTrue(report["entries"][0]["reproduction"]["input_match"])

    def test_argument_error_is_not_a_reproduced_protocol_contract_bug(self):
        batch, _ = self.batch("bad-args", extra=("--unknown-option",))
        report = triage_batch(self.config, batch, self.base / "catalog")
        self.assertEqual(report["reproduced"], 0)
        self.assertEqual(report["unmeasured"], 1, report)

    def test_interrupted_reproduction_is_reserved_and_never_retried_implicitly(self):
        batch, _ = self.batch("interrupted")
        output = self.base / "catalog"
        with mock.patch("shakedown.triage.run_case", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                triage_batch(self.config, batch, output)
        with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("retry after interruption")):
            report = triage_batch(self.config, batch, output)
        self.assertEqual(report["unmeasured"], 1)
        self.assertEqual(report["new_sources"], 0)

    def test_finding_id_must_belong_to_inventory(self):
        batch, record = self.batch("bad-id")
        record["id"] = "../../elsewhere"
        (batch / "findings.jsonl").write_bytes(canonical_json(record) + b"\n")
        with self.assertRaises(ValueError):
            triage_batch(self.config, batch, self.base / "catalog")

    def test_anomaly_signature_keeps_distinct_cohorts_and_signal_kinds(self):
        metadata = {"map": "yard", "profile": "cautious", "variant": "hazard"}
        first = finding_signature(metadata, [], signal="unwarned_explosion_death")
        other = finding_signature(metadata, [], signal="extraction_zone_timeout")
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, finding_signature(metadata, [], signal="unwarned_explosion_death",
                                                     group={"map": "warehouse"}))

    def test_corrupted_catalogue_does_not_duplicate_or_retry_signatures(self):
        batch, _ = self.batch("catalogue")
        output = self.base / "catalog"
        triage_batch(self.config, batch, output)
        path = output / "findings/index.json"
        index = json.loads(path.read_text())
        index["entries"].append(index["entries"][0])
        write_json(path, index)
        with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("retry")):
            with self.assertRaises(ValueError):
                triage_batch(self.config, batch, output)

    def test_missing_retained_reproduction_proof_downgrades_without_retry(self):
        batch, _ = self.batch("missing-proof")
        output = self.base / "catalog"
        original = triage_batch(self.config, batch, output)
        entry = original["entries"][0]
        self.assertEqual(entry["status"], "reproduced")
        shutil.rmtree(output / "findings" / entry["id"])
        with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("implicit proof retry")):
            report = triage_batch(self.config, batch, output)
        self.assertEqual(report["reproduced"], 0)
        self.assertEqual(report["unmeasured"], 1)
        self.assertEqual(report["entries"][0]["historical_status"], "reproduced")
        self.assertEqual(report["entries"][0]["reproduction"]["status"], "unmeasured")
        self.assertTrue(report["entries"][0]["proof_issues"])

    def test_changed_retained_metadata_or_receipt_downgrades_without_retry(self):
        for relative in ("reproduction/run.json", "input-replay/telemetry.jsonl",
                         "bug_draft.json", "reproduction_receipt.json"):
            with self.subTest(relative=relative):
                name = relative.replace("/", "-").replace(".", "-")
                batch, _ = self.batch(name)
                output = self.base / (name + "-catalog")
                original = triage_batch(self.config, batch, output)
                entry = original["entries"][0]
                path = output / "findings" / entry["id"] / relative
                path.write_bytes(path.read_bytes() + b" ")
                with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("implicit proof retry")):
                    report = triage_batch(self.config, batch, output)
                self.assertEqual(report["reproduced"], 0)
                self.assertEqual(report["unmeasured"], 1)
                self.assertTrue(any("changed" in issue for issue in report["entries"][0]["proof_issues"]))

    def test_truncated_proof_inventory_cannot_hide_a_missing_replay(self):
        batch, _ = self.batch("truncated-proof")
        output = self.base / "catalog"
        original = triage_batch(self.config, batch, output)
        entry = original["entries"][0]
        index_path = output / "findings/index.json"
        index = json.loads(index_path.read_text())
        del index["entries"][0]["proof_files"]["input-replay/inputs.json"]
        write_json(index_path, index)
        (output / "findings" / entry["id"] / "input-replay/inputs.json").unlink()
        with mock.patch("shakedown.triage.run_case", side_effect=AssertionError("implicit retry")):
            report = triage_batch(self.config, batch, output)
        self.assertEqual(report["reproduced"], 0)
        self.assertEqual(report["unmeasured"], 1)


if __name__ == "__main__":
    unittest.main()
