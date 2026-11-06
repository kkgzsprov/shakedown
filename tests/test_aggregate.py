"""Hand-calculated evidence, censoring, and seed pairing checks for M2."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import tests  # noqa: F401 - Make the source package importable without installation.

from shakedown.aggregate import aggregate_batch
from shakedown.protocol import canonical_json


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "runs").mkdir()

    def run_fixture(self, name, *, seed=1, decider="rule", outcome="extract", seconds=10,
                    loot=10, counts=None, events=(), tuning=None, **overrides):
        folder = self.base / "runs" / name
        folder.mkdir()
        tuning = tuning or {}
        meta = {"v": 1, "id": name, "seed": seed, "map": "yard", "profile": "cautious",
                "decider": decider, "tuning": tuning,
                "tuning_hash": hashlib.sha256(canonical_json(tuning)).hexdigest(),
                "status": "finding" if outcome == "oracle" else "completed",
                "exit_code": 4 if outcome == "oracle" else 0, "wall_timeout": False,
                "valid": True, "invalid_reasons": []}
        meta.update(overrides)
        (folder / "run.json").write_text(json.dumps(meta), encoding="utf-8")
        rows = [{"type": "run_begin", "step": 0, "t": 0, "seed": seed, "map": "yard"}, *events]
        if outcome == "oracle":
            rows.append({"type": "oracle", "step": seconds * 60, "t": seconds, "oracle": "numeric"})
        rows.append({"type": "run_end", "step": seconds * 60, "t": seconds, "outcome": outcome,
                     "oracle": "numeric" if outcome == "oracle" else None,
                     "counts": counts or {}, "loot": loot})
        self.write_rows(folder / "telemetry.jsonl", rows)
        return folder

    @staticmethod
    def write_rows(path, rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def aggregate(self):
        return aggregate_batch(self.base, bootstrap_samples=200, seed=42)

    def test_cell_means_and_bootstrap_intervals_match_small_dataset(self):
        self.run_fixture("a", seed=1, loot=10, counts={"shots": 4}, seconds=10)
        self.run_fixture("b", seed=2, loot=30, counts={"shots": 6}, seconds=20, outcome="death")
        result = self.aggregate()
        self.assertEqual((result["total_runs"], result["valid_runs"]), (2, 2))
        metrics = result["groups"][0]["metrics"]
        self.assertEqual(metrics["loot"], {"mean": 20.0, "ci95": [10.0, 30.0], "n": 2})
        self.assertEqual(metrics["survival_rate"], {"mean": .5, "ci95": [0.0, 1.0], "n": 2})
        self.assertEqual(metrics["session_seconds"]["mean"], 15)
        self.assertEqual(metrics["shots"]["mean"], 5)
        self.assertEqual(result, json.loads((self.base / "metrics.json").read_text(encoding="utf-8")))
        first_bytes = (self.base / "metrics.json").read_bytes()
        self.assertEqual(result, self.aggregate())
        self.assertEqual(first_bytes, (self.base / "metrics.json").read_bytes())

    def test_no_contacts_and_missing_observability_are_unknown_not_zero(self):
        self.run_fixture("quiet")
        metrics = self.aggregate()["groups"][0]["metrics"]
        for name in ("time_to_first_contact_seconds", "deaths_per_contact", "first_contact_by_player",
                     "hits_from_unseen_shooters", "latency_ms_p50"):
            self.assertEqual(metrics[name], {"mean": None, "ci95": None, "n": 0})
        self.assertEqual(metrics["shots"], {"mean": None, "ci95": None, "n": 0})
        self.assertEqual(metrics["heals"], {"mean": None, "ci95": None, "n": 0})
        self.assertEqual(metrics["decisions"], {"mean": None, "ci95": None, "n": 0})

    def test_supported_counters_distinguish_measured_zero_from_absence(self):
        self.run_fixture("quiet", counts={"shots": 0, "heals": 0})
        metrics = self.aggregate()["groups"][0]["metrics"]
        for name in ("shots", "heals"):
            self.assertEqual(metrics[name], {"mean": 0.0, "ci95": [0.0, 0.0], "n": 1})

    def test_remapped_exit_codes_preserve_normal_and_oracle_observations(self):
        mapping = {"ok": 10, "contract": 20, "oracle": 40, "divergence": 50}
        self.run_fixture("normal", exit_code=10, exit_codes=mapping)
        self.run_fixture("oracle", outcome="oracle", exit_code=40, exit_codes=mapping)
        result = self.aggregate()
        self.assertEqual(result["valid_runs"], 2)
        self.assertEqual(result["groups"][0]["metrics"]["oracle_rate"]["mean"], .5)

    def test_tuning_hash_and_invalid_reason_consistency_are_checked(self):
        self.run_fixture("wrong-hash", tuning_hash="0" * 64)
        self.run_fixture("contradiction", valid=True, invalid_reasons=["contract_error"])
        self.run_fixture("missing-exit")
        path = self.base / "runs" / "missing-exit" / "run.json"
        meta = json.loads(path.read_text())
        del meta["exit_code"]
        path.write_text(json.dumps(meta), encoding="utf-8")
        result = self.aggregate()
        self.assertEqual(result["valid_runs"], 0)
        self.assertEqual(len(result["invalid_runs"]), 3)

    def test_oracle_is_valid_but_censors_survival_and_extraction(self):
        self.run_fixture("oracle", outcome="oracle")
        result = self.aggregate()
        self.assertEqual(result["valid_runs"], 1)
        metrics = result["groups"][0]["metrics"]
        self.assertEqual(metrics["oracle_rate"]["mean"], 1)
        self.assertEqual(metrics["survival_rate"]["n"], 0)
        self.assertEqual(metrics["extraction_rate"]["n"], 0)

    def test_invalid_missing_and_incomplete_runs_stay_in_denominator(self):
        self.run_fixture("good")
        self.run_fixture("bad", valid=False, exit_code=2, invalid_reasons=["contract_error"])
        self.run_fixture("running", status="running")
        (self.base / "runs" / "missing").mkdir()
        result = self.aggregate()
        self.assertEqual((result["total_runs"], result["valid_runs"]), (4, 1))
        self.assertEqual({row["id"] for row in result["invalid_runs"]}, {"bad", "missing", "running"})
        self.assertEqual(result["groups"][0]["runs"], 1)

    def test_malformed_nonfinite_and_contradictory_telemetry_are_invalid(self):
        missing_end = self.run_fixture("missing-end")
        self.write_rows(missing_end / "telemetry.jsonl", [{"type": "run_begin", "step": 0, "t": 0}])
        nonfinite = self.run_fixture("nonfinite")
        (nonfinite / "telemetry.jsonl").write_text('{"type":"run_begin","step":0,"t":NaN}\n', encoding="utf-8")
        disagreement = self.run_fixture("exit-disagrees")
        rows = [json.loads(line) for line in (disagreement / "telemetry.jsonl").read_text().splitlines()]
        rows[-1]["outcome"] = "oracle"
        self.write_rows(disagreement / "telemetry.jsonl", rows)
        result = self.aggregate()
        self.assertEqual(result["total_runs"], 3)
        self.assertEqual(len(result["invalid_runs"]), 3)
        self.assertEqual(result["groups"], [])

    def test_pairing_uses_common_seed_identifiers_not_run_order(self):
        self.run_fixture("left-1", seed=1, decider="mock", loot=10)
        self.run_fixture("left-2", seed=2, decider="mock", loot=20)
        self.run_fixture("right-3", seed=3, decider="rule", loot=90)
        self.run_fixture("right-2", seed=2, decider="rule", loot=50)
        comparison = self.aggregate()["comparisons"][0]
        self.assertEqual(comparison["axis"], "decider")
        self.assertEqual(comparison["paired_seeds"], [2])
        self.assertEqual(comparison["metrics"]["loot"], {"mean": 30.0, "ci95": [30.0, 30.0], "n": 1})

    def test_duplicate_seed_is_explicitly_excluded_from_paired_inference(self):
        self.run_fixture("left-a", seed=1, decider="mock", loot=1)
        self.run_fixture("left-b", seed=1, decider="mock", loot=100)
        self.run_fixture("left-c", seed=2, decider="mock", loot=20)
        self.run_fixture("right-a", seed=1, decider="rule", loot=200)
        self.run_fixture("right-b", seed=2, decider="rule", loot=30)
        result = self.aggregate()
        self.assertEqual(result["groups"][0]["runs"], 3)
        comparison = result["comparisons"][0]
        self.assertEqual(comparison["ambiguous_seeds"], [1])
        self.assertEqual(comparison["paired_seeds"], [2])
        self.assertEqual(comparison["metrics"]["loot"]["mean"], 10)

    def test_tuning_pair_is_allowed_but_joint_policy_and_tuning_change_is_not(self):
        self.run_fixture("base", decider="rule")
        self.run_fixture("tuned", decider="rule", tuning={"player.speed": 4})
        result = self.aggregate()
        self.assertEqual(len(result["comparisons"]), 1)
        self.assertEqual(result["comparisons"][0]["axis"], "tuning")
        self.run_fixture("different-policy-and-tuning", decider="mock", tuning={"player.speed": 5})
        self.assertEqual(len(self.aggregate()["comparisons"]), 1)

    def test_latency_dropped_decisions_and_repeated_tactics_use_real_records(self):
        events = [{"type": "contact", "step": 30, "t": .5}]
        for seq, choice in enumerate(("cover", "cover", "hold", "hold"), 1):
            events.append({"type": "decision", "step": seq * 60, "t": seq,
                           "seq": seq, "choice": choice, "status": "selected"})
        events.append({"type": "decision", "step": 250, "t": 250 / 60,
                       "seq": 4, "choice": "hold", "status": "dropped"})
        folder = self.run_fixture("calls", decider="mock", events=events, outcome="death")
        self.write_rows(folder / "decision_tape.jsonl", [
            {"seq": seq, "choice": choice, "source": "mock", "latency_ms": seq * 10}
            for seq, choice in enumerate(("cover", "cover", "hold", "hold"), 1)])
        metrics = self.aggregate()["groups"][0]["metrics"]
        for metric, expected in {"decisions": 4, "latency_ms_p50": 20, "latency_ms_p95": 40,
                                 "longest_repeated_tactic": 2, "dropped_decision_rate": .25,
                                 "time_to_first_contact_seconds": .5, "deaths_per_contact": 1}.items():
            self.assertEqual(metrics[metric]["mean"], expected, metric)

    def test_invalid_bridge_summary_is_excluded_even_when_run_metadata_says_valid(self):
        folder = self.run_fixture("fallback", decider="mock")
        (folder / "bridge_summary.json").write_text(json.dumps({
            "valid": False, "fallback_rate": .2, "exit_code": 0, "wall_timeout": False,
            "end": {"outcome": "extract", "step": 600}, "decisions": 0}), encoding="utf-8")
        result = self.aggregate()
        self.assertEqual(result["valid_runs"], 0)
        self.assertEqual(len(result["invalid_runs"]), 1)

    def test_empty_batch_and_invalid_bootstrap_arguments(self):
        result = self.aggregate()
        self.assertEqual(result["total_runs"], 0)
        self.assertEqual(result["groups"], [])
        for samples in (0, -1, True, 1.5):
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                aggregate_batch(self.base, bootstrap_samples=samples)

    def inventory(self, *names, source="planned_runs", **overrides):
        descriptors = []
        for name in names:
            path = self.base / "runs" / name / "run.json"
            if path.exists():
                meta = json.loads(path.read_text(encoding="utf-8"))
            else:
                meta = {"id": name, "seed": 1, "map": "yard", "profile": "cautious",
                        "decider": "rule", "tuning_hash": hashlib.sha256(canonical_json({})).hexdigest()}
            descriptors.append({key: meta[key] for key in
                                ("id", "seed", "map", "profile", "decider", "tuning_hash")})
        manifest = {"v": 1, "total_runs": len(descriptors), source: descriptors}
        manifest.update(overrides)
        (self.base / "batch.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_planned_run_with_missing_whole_directory_stays_in_denominator(self):
        self.run_fixture("a")
        self.inventory("a", "b")
        result = self.aggregate()
        self.assertEqual((result["total_runs"], result["valid_runs"]), (2, 1))
        self.assertEqual(result["inventory_source"], "planned_runs")
        self.assertEqual(result["invalid_runs"], [
            {"id": "b", "path": "runs/b", "reasons": ["missing_run_directory"]}])

    def test_final_run_inventory_is_supported_and_all_missing_runs_are_retained(self):
        self.inventory("a", "b", source="runs")
        (self.base / "runs").rmdir()
        result = self.aggregate()
        self.assertEqual(result["total_runs"], 2)
        self.assertEqual(result["valid_runs"], 0)
        self.assertEqual(result["inventory_source"], "runs")
        self.assertEqual({row["id"] for row in result["invalid_runs"]}, {"a", "b"})

    def test_inventory_disagreement_invalidates_a_run_without_losing_its_slot(self):
        self.run_fixture("a")
        self.inventory("a")
        path = self.base / "runs" / "a" / "run.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["profile"] = "aggressive"
        path.write_text(json.dumps(meta), encoding="utf-8")
        result = self.aggregate()
        self.assertEqual((result["total_runs"], result["valid_runs"]), (1, 0))
        self.assertIn("run_differs_from_inventory", result["invalid_runs"][0]["reasons"])

    def test_malformed_and_inconsistent_inventory_are_explicit_errors(self):
        self.run_fixture("a")
        for fault in ("duplicate", "count", "unsafe-id", "descriptor", "unexpected-folder"):
            with self.subTest(fault=fault):
                manifest = self.inventory("a")
                if fault == "duplicate":
                    manifest["planned_runs"].append(dict(manifest["planned_runs"][0]))
                    manifest["total_runs"] = 2
                elif fault == "count":
                    manifest["total_runs"] = 2
                elif fault == "unsafe-id":
                    manifest["planned_runs"][0]["id"] = "../elsewhere"
                elif fault == "descriptor":
                    del manifest["planned_runs"][0]["seed"]
                else:
                    manifest["planned_runs"][0]["id"] = "b"
                (self.base / "batch.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "invalid batch inventory"):
                    self.aggregate()

    def test_initial_inventory_remains_authoritative_while_completed_runs_are_partial(self):
        self.run_fixture("a")
        self.inventory("a", "b", runs=[])
        result = self.aggregate()
        self.assertEqual(result["total_runs"], 2)
        self.assertEqual(result["valid_runs"], 1)


if __name__ == "__main__":
    unittest.main()
