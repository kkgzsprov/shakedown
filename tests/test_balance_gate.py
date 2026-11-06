"""Measured holdout improvement never substitutes for missing safety evidence."""

from copy import deepcopy
import unittest

import tests  # noqa: F401

from shakedown.balance_gate import evaluate_gate
from shakedown.orchestrate import digest


BASELINE = {"speed": 3.0}
CANDIDATE = {"speed": 5.0}
SCOPES = [("cautious", "yard")]
POLICIES = ["rule", "mock"]
TARGETS = [{"metric": "loot", "profile": "cautious", "map": "yard", "min": 50, "max": 60}]


def estimate(value, n=5):
    return {"mean": value, "ci95": [value, value] if value is not None else None, "n": n}


def evidence():
    groups = []
    for tuning, loot in ((BASELINE, 10), (CANDIDATE, 50)):
        for policy in POLICIES:
            groups.append({"profile": "cautious", "map": "yard", "decider": policy,
                           "tuning_hash": digest(tuning), "runs": 5,
                           "metrics": {"loot": estimate(loot), "oracle_rate": estimate(0)}})
    return {"v": 1, "invalid_runs": [], "groups": groups}


class BalanceGateTests(unittest.TestCase):
    def setUp(self):
        self.metrics = evidence()

    def gate(self, **kwargs):
        arguments = {"baseline": BASELINE, "candidate": CANDIDATE,
                     "scopes": SCOPES, "policies": POLICIES}
        arguments.update(kwargs)
        return evaluate_gate(self.metrics, TARGETS, **arguments)

    def group(self, tuning=CANDIDATE, policy="rule"):
        return next(group for group in self.metrics["groups"]
                    if group["tuning_hash"] == digest(tuning) and group["decider"] == policy)

    def test_pass_requires_measured_improvement_and_preserves_inputs(self):
        before = deepcopy(self.metrics)
        result = self.gate()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["proposed"])
        self.assertEqual(result["baseline_score"], 80)
        self.assertEqual(result["candidate_score"], 0)
        self.assertEqual(result["gain_fraction"], 1)
        self.assertFalse(result["policy_sensitive"])
        self.assertEqual(result["checked_policies"], POLICIES)
        self.assertEqual(len(result["oracle_comparisons"]), 2)
        self.assertEqual(self.metrics, before)

    def test_min_gain_failure_is_measured_rejection(self):
        for policy in POLICIES:
            self.group(policy=policy)["metrics"]["loot"] = estimate(11)
        result = self.gate()
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["proposed"])
        self.assertTrue(any("min_gain" in reason for reason in result["reasons"]))

    def test_oracle_regression_blocks_even_perfect_targets(self):
        self.group()["metrics"]["oracle_rate"] = estimate(.2)
        result = self.gate()
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("oracle rate increased" in reason for reason in result["reasons"]))

    def test_group_runs_do_not_replace_target_measurement_count(self):
        self.group()["metrics"]["loot"] = estimate(50, 4)
        result = self.gate()
        self.assertEqual(result["status"], "unmeasured")
        self.assertFalse(result["proposed"])

    def test_missing_target_or_oracle_observations_stay_unmeasured(self):
        for metric in ("loot", "oracle_rate"):
            with self.subTest(metric=metric):
                self.metrics = evidence()
                self.group()["metrics"][metric] = estimate(None, 0)
                result = self.gate()
                self.assertEqual(result["status"], "unmeasured")
                self.assertFalse(result["proposed"])

    def test_invalid_executions_are_rejected(self):
        self.metrics["invalid_runs"] = [{"id": "r000001", "reasons": ["wall_timeout"]}]
        self.assertEqual(self.gate()["status"], "rejected")

    def test_single_policy_does_not_certify_policy_sensitivity(self):
        result = self.gate(policies=["rule"])
        self.assertEqual(result["status"], "unmeasured")
        self.assertIsNone(result["policy_sensitive"])
        self.assertFalse(result["proposed"])

    def test_preference_reversal_blocks_a_combined_score_gain(self):
        self.group(BASELINE, "mock")["metrics"]["loot"] = estimate(50)
        self.group(CANDIDATE, "mock")["metrics"]["loot"] = estimate(49)
        result = self.gate()
        self.assertLess(result["candidate_score"], result["baseline_score"])
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(result["policy_sensitive"])

    def test_zero_baseline_distance_is_not_a_new_proposal(self):
        for policy in POLICIES:
            self.group(BASELINE, policy)["metrics"]["loot"] = estimate(50)
        result = self.gate()
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["gain_fraction"])

    def test_duplicate_group_is_not_silently_pooled(self):
        self.metrics["groups"].append(deepcopy(self.group()))
        result = self.gate()
        self.assertEqual(result["status"], "unmeasured")
        self.assertFalse(result["proposed"])

    def test_malformed_group_or_inventory_cannot_pass(self):
        for change in (lambda: self.metrics.pop("invalid_runs"),
                       lambda: self.metrics["groups"].append([]),
                       lambda: self.group().update(runs=True),
                       lambda: self.group()["metrics"].update(oracle_rate=estimate(float("nan"))),
                       lambda: self.group()["metrics"]["oracle_rate"].update(n=4),
                       lambda: self.group()["metrics"]["oracle_rate"].update(ci95=[1, 0])):
            with self.subTest(change=change):
                self.metrics = evidence()
                change()
                result = self.gate()
                self.assertIn(result["status"], {"unmeasured", "rejected"})
                self.assertFalse(result["proposed"])

    def test_configuration_errors_fail_before_evidence_evaluation(self):
        for options in ({"min_runs": 0}, {"min_runs": True}, {"min_gain": 0},
                        {"min_gain": float("nan")}, {"policies": ["rule", "rule"]},
                        {"scopes": SCOPES * 2}, {"scopes": [["cautious"]]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.gate(**options)

    def test_nonobject_measurement_evidence_is_unmeasured(self):
        for malformed in ([], None, {}, {"groups": None}):
            with self.subTest(malformed=malformed):
                self.metrics = malformed
                self.assertEqual(self.gate()["status"], "unmeasured")


if __name__ == "__main__":
    unittest.main()
