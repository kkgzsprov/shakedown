"""Opportunity denominators, Wilson evidence, and honest missing measurements."""

import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

import tests  # noqa: F401
from shakedown.anomalies import analyze_batch, signal_observation, wilson_lower
from shakedown.protocol import canonical_json


class AnomalyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.batch = self.root / "batch"
        self.baseline = self.root / "baseline"
        for directory in (self.batch, self.baseline):
            (directory / "runs").mkdir(parents=True)

    def run_fixture(self, name, *, root=None, variant="default", outcome="extract",
                    counts=None, events=(), end_fields=None, **metadata):
        directory = (root or self.batch) / "runs" / name
        directory.mkdir()
        meta = {"v": 1, "id": name, "seed": int(name[-2:]) if name[-2:].isdigit() else 1,
                "map": "yard", "profile": "cautious", "decider": "rule", "variant": variant,
                "tuning": {}, "tuning_hash": hashlib.sha256(canonical_json({})).hexdigest(),
                "status": "finding" if outcome == "oracle" else "completed",
                "exit_code": 4 if outcome == "oracle" else 0, "wall_timeout": False,
                "valid": True, "invalid_reasons": []}
        meta.update(metadata)
        (directory / "run.json").write_bytes(canonical_json(meta) + b"\n")
        rows = [{"type": "run_begin", "step": 0, "t": 0, "seed": meta["seed"], "map": "yard"}]
        rows.extend(events)
        if outcome == "oracle":
            rows.append({"type": "oracle", "step": 600, "t": 10, "oracle": "numeric"})
        rows.append({"type": "run_end", "step": 600, "t": 10, "outcome": outcome,
                     "oracle": "numeric" if outcome == "oracle" else None,
                     "counts": counts or {}, **(end_fields or {})})
        (directory / "telemetry.jsonl").write_bytes(b"".join(canonical_json(row) + b"\n" for row in rows))
        return directory, rows

    def explosions(self, *, warned, count=8, root=None, variant="grenade", death=True):
        for index in range(count):
            events = [{"type": "explosion", "step": 60, "t": 1, "warned": warned}]
            if death:
                events.append({"type": "death", "step": 60, "t": 1,
                               "cause": "explosion", "warned": warned})
            self.run_fixture(f"e{index:02d}", root=root, variant=variant,
                             outcome="death" if death else "extract", events=events)

    @staticmethod
    def signal(report, signal_id, variant="default"):
        return next(row for row in report["signals"]
                    if row["id"] == signal_id and row["group"]["variant"] == variant)

    def test_wilson_lower_matches_hand_calculated_examples_and_bounds(self):
        self.assertIsNone(wilson_lower(0, 0))
        self.assertEqual(wilson_lower(0, 10), 0)
        self.assertAlmostEqual(wilson_lower(5, 10), .2365895936154873)
        self.assertAlmostEqual(wilson_lower(10, 10), 10 / (10 + 1.96**2))
        for n in (1, 5, 20):
            values = [wilson_lower(hits, n) for hits in range(n + 1)]
            self.assertEqual(values, sorted(values))
            self.assertTrue(all(0 <= lower <= hits / n for hits, lower in enumerate(values)))

    def test_unwarned_deaths_flag_only_with_measured_baseline_opportunities(self):
        self.explosions(warned=False)
        self.explosions(warned=True, root=self.baseline, death=False)
        report = analyze_batch(self.batch, baseline_dir=self.baseline)
        signal = self.signal(report, "unwarned_explosion_death", "grenade")
        self.assertEqual((signal["hits"], signal["n"], signal["baseline_hits"], signal["baseline_n"]),
                         (8, 8, 0, 8))
        self.assertEqual(signal["status"], "flagged")
        finding = next(row for row in report["findings"] if row["signal"] == signal["id"])
        self.assertEqual(finding["mechanism"], "anomaly")
        self.assertEqual(finding["run_ids"], [f"e{index:02d}" for index in range(8)])
        self.assertEqual(report, json.loads((self.batch / "anomalies.json").read_text()))
        self.assertIn("not confirmed bugs", (self.batch / "anomalies.md").read_text())

    def test_one_group_does_not_flag_itself_against_its_own_rate(self):
        self.explosions(warned=False)
        report = analyze_batch(self.batch)
        signal = self.signal(report, "unwarned_explosion_death", "grenade")
        self.assertEqual(signal["overall_rate"], 1)
        self.assertEqual(signal["status"], "clear")
        self.assertFalse(report["findings"])

    def test_minimum_runs_suppresses_even_a_perfect_raw_rate(self):
        self.explosions(warned=False, count=4)
        self.explosions(warned=True, count=8, root=self.baseline, death=False)
        report = analyze_batch(self.batch, baseline_dir=self.baseline)
        signal = self.signal(report, "unwarned_explosion_death", "grenade")
        self.assertEqual(signal["rate"], 1)
        self.assertEqual(signal["status"], "unmeasured")
        self.assertEqual(signal["reason"], "insufficient_runs")

    def test_missing_baseline_opportunities_are_not_an_assumed_zero(self):
        self.explosions(warned=False)
        for index in range(8):
            self.run_fixture(f"q{index:02d}", root=self.baseline)
        report = analyze_batch(self.batch, baseline_dir=self.baseline)
        signal = self.signal(report, "unwarned_explosion_death", "grenade")
        self.assertEqual(signal["status"], "unmeasured")
        self.assertIsNone(signal["overall_rate"])
        self.assertEqual(signal["baseline_n"], 0)

    def test_extraction_requires_an_attempt_and_measured_zone_dwell(self):
        for index in range(8):
            self.run_fixture(f"x{index:02d}", outcome="timeout", variant="extract",
                             end_fields={"extraction_attempts": 60, "extract_zone_seconds": 8,
                                         "in_extraction_zone": True})
            self.run_fixture(f"x{index:02d}", root=self.baseline, variant="extract",
                             events=[{"type": "extract", "step": 60, "t": 1}],
                             end_fields={"extraction_attempts": 60, "extract_zone_seconds": 1})
        self.run_fixture("quiet", outcome="timeout", variant="quiet")
        report = analyze_batch(self.batch, baseline_dir=self.baseline)
        signal = self.signal(report, "extraction_zone_timeout", "extract")
        self.assertEqual((signal["hits"], signal["n"], signal["status"]), (8, 8, "flagged"))
        quiet = self.signal(report, "extraction_zone_timeout", "quiet")
        self.assertEqual((quiet["n"], quiet["status"]), (0, "unmeasured"))
        self.assertIsNone(quiet["rate"])

    def test_historical_extraction_attempt_does_not_mean_timeout_ended_inside(self):
        for index in range(8):
            self.run_fixture(f"x{index:02d}", outcome="timeout", variant="left-zone",
                             end_fields={"extraction_attempts": 60, "extract_zone_seconds": 8,
                                         "in_extraction_zone": False})
            self.run_fixture(f"u{index:02d}", outcome="timeout", variant="unknown-zone",
                             end_fields={"extraction_attempts": 60, "extract_zone_seconds": 8})
        report = analyze_batch(self.batch)
        outside = self.signal(report, "extraction_zone_timeout", "left-zone")
        self.assertEqual((outside["hits"], outside["n"], outside["status"]), (0, 8, "clear"))
        unknown = self.signal(report, "extraction_zone_timeout", "unknown-zone")
        self.assertEqual((unknown["n"], unknown["status"]), (0, "unmeasured"))
        self.assertIsNone(unknown["rate"])
        _, rows = self.run_fixture("nonbool", outcome="timeout", end_fields={
            "extraction_attempts": 60, "extract_zone_seconds": 8, "in_extraction_zone": 1})
        self.assertIsNone(signal_observation("extraction_zone_timeout", {}, rows))

    def test_counter_cannot_erase_explicit_unwarned_explosion_deaths(self):
        contradictory_rows = None
        for index in range(8):
            _, contradictory_rows = self.run_fixture(f"b{index:02d}", variant="grenade",
                outcome="death", counts={"explosions": 1, "unwarned_explosion_deaths": 0},
                events=[{"type": "explosion", "step": 60, "t": 1, "warned": False},
                        {"type": "death", "step": 60, "t": 1,
                         "cause": "explosion", "warned": False}])
        self.explosions(warned=True, root=self.baseline, death=False)
        report = analyze_batch(self.batch, baseline_dir=self.baseline)
        self.assertEqual((report["total_runs"], report["valid_runs"]), (8, 0))
        self.assertEqual(len(report["invalid_runs"]), 8)
        self.assertTrue(all("unwarned death count contradicts" in row["reasons"][0]
                            for row in report["invalid_runs"]))
        self.assertFalse(report["signals"])
        self.assertFalse(report["findings"])
        with self.assertRaisesRegex(ValueError, "unwarned death count contradicts"):
            signal_observation("unwarned_explosion_death", {}, contradictory_rows)

    def test_quiet_clean_runs_have_no_shot_or_loot_failure_without_opportunities(self):
        for index in range(8):
            self.run_fixture(f"q{index:02d}", counts={"contacts": 0, "shots": 0, "loot": 0,
                                                     "loot_opportunities": 0})
        report = analyze_batch(self.batch)
        for signal_id in ("no_shots", "no_loot", "unwarned_explosion_death", "extraction_zone_timeout"):
            self.assertEqual(self.signal(report, signal_id)["n"], 0)
        self.assertFalse(report["findings"])

    def test_no_shots_and_no_loot_use_instrumented_opportunities(self):
        for index in range(20):
            self.run_fixture(f"n{index:02d}", counts={"contacts": 1, "shots": 0, "loot_opportunities": 1,
                                                     "loot": 0},
                             events=[{"type": "contact", "step": 180, "t": 3}])
            self.run_fixture(f"n{index:02d}", root=self.baseline,
                             counts={"contacts": 1, "shots": 3, "loot_opportunities": 1, "loot": 10},
                             events=[{"type": "contact", "step": 180, "t": 3}])
        report = analyze_batch(self.batch, baseline_dir=self.baseline)
        for signal_id in ("no_shots", "no_loot"):
            self.assertEqual(self.signal(report, signal_id)["status"], "flagged")

    def test_invalid_oracle_and_missing_evidence_do_not_manufacture_success(self):
        self.explosions(warned=False)
        self.explosions(warned=True, root=self.baseline, death=False)
        self.run_fixture("invalid", variant="grenade", exit_code=2, valid=False,
                         invalid_reasons=["contract_error"])
        self.run_fixture("oracle", variant="grenade", outcome="oracle", counts={"explosions": 1,
                         "unwarned_explosion_deaths": 1})
        (self.batch / "runs" / "missing").mkdir()
        report = analyze_batch(self.batch, baseline_dir=self.baseline)
        self.assertEqual((report["total_runs"], report["valid_runs"]), (11, 9))
        self.assertEqual({row["id"] for row in report["invalid_runs"]}, {"invalid", "missing"})
        self.assertEqual(self.signal(report, "unwarned_explosion_death", "grenade")["n"], 8)

    def test_inventory_retains_a_whole_missing_run_directory(self):
        directory, _ = self.run_fixture("present")
        meta = json.loads((directory / "run.json").read_text())
        planned = [meta, {**meta, "id": "absent", "seed": 2}]
        (self.batch / "batch.json").write_bytes(canonical_json({"v": 1, "total_runs": 2,
                                                               "planned_runs": planned}))
        report = analyze_batch(self.batch)
        self.assertEqual((report["total_runs"], report["valid_runs"]), (2, 1))
        self.assertEqual(report["invalid_runs"][0]["id"], "absent")

    def test_death_causes_have_separate_explicit_conditional_denominators(self):
        for index in range(8):
            self.run_fixture(f"b{index:02d}", outcome="death", events=[
                {"type": "death", "step": 60, "t": 1, "cause": "bullet"}])
            self.run_fixture(f"e{index:02d}", root=self.baseline, outcome="death", events=[
                {"type": "death", "step": 60, "t": 1, "cause": "explosion", "warned": True}])
        report = analyze_batch(self.batch, baseline_dir=self.baseline)
        bullets = self.signal(report, "cause_share:bullet")
        self.assertEqual((bullets["hits"], bullets["n"], bullets["status"]), (8, 8, "flagged"))
        explosions = self.signal(report, "cause_share:explosion")
        self.assertEqual((explosions["hits"], explosions["n"], explosions["status"]), (0, 8, "clear"))

    def test_public_reproduction_predicate_is_telemetry_based(self):
        _, rows = self.run_fixture("death", outcome="death", events=[
            {"type": "death", "step": 60, "t": 1, "cause": "explosion", "warned": False}])
        self.assertIs(signal_observation("unwarned_explosion_death", {"bug": "none"}, rows), True)
        self.assertIs(signal_observation("cause_share:explosion", {}, rows), True)
        self.assertIsNone(signal_observation("extraction_zone_timeout", {}, rows))
        with self.assertRaises(ValueError):
            signal_observation("invented", {}, rows)

    def test_arguments_reject_ambiguous_or_nonfinite_values(self):
        for kwargs in ({"min_runs": True}, {"min_runs": 0}, {"thresholds": {"invented": .5}},
                       {"thresholds": {"timeout": True}}, {"thresholds": {"timeout": math.nan}},
                       {"thresholds": {"timeout": -1}}, {"group_by": ("bug",)},
                       {"group_by": ("map", "map")}, {"baseline_dir": self.batch}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                analyze_batch(self.batch, **kwargs)
        for counts in ((True, 1), (2, 1), (-1, 1), (0, -1)):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                wilson_lower(*counts)


if __name__ == "__main__":
    unittest.main()
