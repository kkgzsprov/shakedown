"""Bounded numeric search and target distances preserve missing evidence."""

from copy import deepcopy
import hashlib
import unittest

import tests  # noqa: F401

from shakedown.optimize import candidates, defaults, target_score
from shakedown.protocol import canonical_json


TABLE = {"keys": {"speed": {"type": "float", "min": 1., "max": 5., "default": 3.},
                  "count": {"type": "int", "min": 1, "max": 5, "baseline": 2},
                  "fixed": {"type": "float", "min": 0., "max": 1., "default": .5}}}
RANGES = {"speed": {"min": 1., "max": 5.}, "count": {"min": 1, "max": 5}}


def group(profile="cautious", map_name="yard", policy="rule", value=10, *, n=5):
    return {"profile": profile, "map": map_name, "decider": policy, "tuning_hash": "candidate",
            "runs": 5, "metrics": {"loot": {"mean": value, "ci95": [value, value], "n": n}}}


class CandidateTests(unittest.TestCase):
    def test_documented_defaults_and_baseline_values_are_validated(self):
        self.assertEqual(defaults(TABLE), {"count": 2, "fixed": .5, "speed": 3.})
        for bad in ({"keys": {"x": {"type": "int", "min": 0, "max": 2}}},
                    {"keys": {"x": {"type": "int", "min": 0, "max": 2, "default": True}}},
                    {"keys": {"x": {"type": "float", "min": 0, "max": 2, "default": 3}}}):
            with self.subTest(table=bad), self.assertRaises(ValueError):
                defaults(bad)

    def test_grid_order_endpoints_frozen_keys_and_input_preservation(self):
        original = deepcopy(TABLE)
        result = candidates(TABLE, RANGES, method="grid", count=9)
        self.assertEqual(result[0], {"count": 1, "fixed": .5, "speed": 1.})
        self.assertEqual(result[-1], {"count": 5, "fixed": .5, "speed": 5.})
        self.assertTrue(all(row["fixed"] == .5 for row in result))
        self.assertEqual(TABLE, original)
        self.assertEqual(candidates(TABLE, RANGES, method="grid", count=9), result)

    def test_random_is_seeded_unique_bounded_and_excludes_baseline(self):
        result = candidates(TABLE, RANGES, count=12, seed=17)
        self.assertEqual(result, candidates(TABLE, RANGES, count=12, seed=17))
        self.assertNotEqual(result, candidates(TABLE, RANGES, count=12, seed=18))
        self.assertEqual(len({canonical_json(row) for row in result}), 12)
        for row in result:
            self.assertEqual(type(row["count"]), int)
            self.assertTrue(1 <= row["count"] <= 5 and 1 <= row["speed"] <= 5)
            self.assertEqual(row["fixed"], .5)

    def test_exclusion_accepts_tunings_or_hashes_and_saturates_small_space(self):
        table = {"keys": {"x": {"type": "int", "min": 0, "max": 2, "default": 0}}}
        ranges = {"x": {"min": 0, "max": 2}}
        for method in ("grid", "random"):
            self.assertEqual(candidates(table, ranges, method=method, count=100), [{"x": 1}, {"x": 2}])
            excluded = hashlib.sha256(canonical_json({"x": 1})).hexdigest()
            self.assertEqual(candidates(table, ranges, method=method, count=100,
                                        exclude=[excluded, {"x": 2}]), [])

    def test_large_integer_bounds_are_exact_without_float_round_trip(self):
        low = 2 ** 70 + 1
        table = {"keys": {"x": {"type": "int", "min": low, "max": low + 2, "default": low + 1}}}
        ranges = {"x": {"min": low, "max": low + 2}}
        self.assertEqual(candidates(table, ranges, method="grid", count=5), [{"x": low}, {"x": low + 2}])
        self.assertEqual({row["x"] for row in candidates(table, ranges, count=10)}, {low, low + 2})

    def test_lazy_cartesian_space_is_bounded(self):
        table = {"keys": {f"key{i}": {"type": "int", "min": 0, "max": 10**20, "default": 1}
                          for i in range(30)}}
        ranges = {key: {"min": 0, "max": 10**20} for key in table["keys"]}
        result = candidates(table, ranges, method="grid", count=2, grid_points=10**10)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["key0"], 0)

    def test_large_float_endpoints_do_not_overflow_interpolation(self):
        table = {"keys": {"x": {"type": "float", "min": -1e308, "max": 1e308, "default": 0.}}}
        ranges = {"x": {"min": -1e308, "max": 1e308}}
        self.assertEqual(candidates(table, ranges, method="grid", count=4), [{"x": -1e308}, {"x": 1e308}])
        self.assertEqual(len(candidates(table, ranges, count=3)), 3)

    def test_invalid_ranges_and_parameters_fail_before_search(self):
        for ranges in ({"unknown": {"min": 0, "max": 1}}, {"count": {"min": 1., "max": 2}},
                       {"count": {"min": 4, "max": 2}}, {"speed": {"min": 0, "max": 3}},
                       {"speed": {"min": 1, "max": float("inf")}}, {"speed": {"min": 1}}):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                candidates(TABLE, ranges)
        for kwargs in ({"method": "other"}, {"method": []}, {"count": True}, {"count": -1}, {"seed": True},
                       {"grid_points": 1}, {"baseline": {"count": 9}}, {"exclude": ["bad"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                candidates(TABLE, RANGES, **kwargs)
        self.assertEqual(candidates(TABLE, {}), [])
        self.assertEqual(candidates(TABLE, RANGES, count=0), [])

    def test_malformed_table_and_exclusions_raise_value_errors(self):
        for table in ({"keys": {"x": {"type": [], "min": 0, "max": 1, "default": 0}}},
                      {"keys": {"x": {"type": "float", "min": 0, "max": 10**1000, "default": 0}}}):
            with self.subTest(table=table), self.assertRaises(ValueError):
                defaults(table)
        with self.assertRaises(ValueError):
            candidates(TABLE, RANGES, exclude=None)


class TargetScoreTests(unittest.TestCase):
    def setUp(self):
        self.targets = [{"metric": "loot", "profile": "cautious", "map": None, "min": 15, "max": 20}]
        self.scopes = [("cautious", "yard"), ("cautious", "warehouse")]
        self.policies = ["rule", "mock"]
        self.metrics = {"groups": [group(map_name=map_name, policy=policy, value=value)
                                   for map_name in ("yard", "warehouse")
                                   for policy, value in (("rule", 10), ("mock", 25))]}

    def score(self, metrics=None, targets=None, **kwargs):
        options = {"tuning_hash": "candidate", "scopes": self.scopes,
                   "policies": self.policies, "min_runs": 5, **kwargs}
        return target_score(self.metrics if metrics is None else metrics,
                            self.targets if targets is None else targets, **options)

    def test_map_wildcard_expands_and_sums_raw_distances_per_policy(self):
        result = self.score()
        self.assertEqual(result["status"], "measured")
        self.assertEqual(result["score"], 20)
        self.assertEqual(result["per_policy"], {"rule": 10, "mock": 10})
        self.assertEqual(len(result["targets"]), 4)
        self.assertEqual(self.score(targets={"targets": self.targets}), result)

    def test_inside_target_bounds_has_zero_distance(self):
        for row in self.metrics["groups"]:
            row["metrics"]["loot"].update(mean=15, ci95=[14, 16])
        self.assertEqual(self.score()["score"], 0)

    def test_missing_duplicate_and_censored_evidence_remains_unmeasured(self):
        for mutation in (lambda rows: rows.pop(), lambda rows: rows.append(deepcopy(rows[0])),
                         lambda rows: rows[0]["metrics"]["loot"].update(n=0, mean=None, ci95=None),
                         lambda rows: rows[0]["metrics"]["loot"].update(n=4),
                         lambda rows: rows[0]["metrics"]["loot"].update(n=6),
                         lambda rows: rows[0]["metrics"]["loot"].update(ci95=None),
                         lambda rows: rows[0]["metrics"]["loot"].update(ci95=[20, 10])):
            metrics = deepcopy(self.metrics)
            mutation(metrics["groups"])
            result = self.score(metrics=metrics)
            self.assertEqual(result["status"], "unmeasured")
            self.assertIsNone(result["score"])
            self.assertTrue(result["reasons"])

    def test_malformed_and_nonfinite_metrics_cannot_become_zero_score(self):
        for metrics in ({}, {"groups": []}, {"groups": [None]}, {"groups": [{"tuning_hash": "candidate"}]},
                        {"groups": [group(value=float("nan"))]}, {"groups": [group(value=True)]}):
            with self.subTest(metrics=metrics):
                result = self.score(metrics=metrics)
                self.assertEqual(result["status"], "unmeasured")
                self.assertIsNone(result["score"])

    def test_target_and_scope_mistakes_fail_explicitly(self):
        for targets in ([], self.targets * 2,
                        [{**self.targets[0], "metric": "unknown"}],
                        [{**self.targets[0], "min": 21}],
                        [{**self.targets[0], "map": "unknown"}],
                        [{**self.targets[0], "profile": "unknown"}],
                        [{**self.targets[0], "max": float("inf")}]):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                self.score(targets=targets)
        for kwargs in ({"scopes": []}, {"scopes": self.scopes * 2}, {"policies": []},
                       {"policies": ["rule", "rule"]}, {"min_runs": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.score(**kwargs)

    def test_overflowing_distance_and_total_remain_unmeasured(self):
        for row in self.metrics["groups"]:
            row["metrics"]["loot"]["mean"] = float(row["metrics"]["loot"]["mean"])
        target = [{**self.targets[0], "min": 10**1000, "max": 10**1000 + 1}]
        self.assertEqual(self.score(targets=target)["status"], "unmeasured")
        target = [{**self.targets[0], "min": 1e308, "max": 1e308}]
        self.assertEqual(self.score(targets=target)["status"], "unmeasured")


if __name__ == "__main__":
    unittest.main()
