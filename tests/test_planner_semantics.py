"""Configuration-bound drafts and faithful measured planner results."""

from copy import deepcopy
import unittest

import tests  # noqa: F401

from shakedown.planner.mock import generate
from shakedown.planner.schema import STEPS, from_wire, to_wire, validate_output
from shakedown.planner.semantics import normalize


def context():
    value = {"profiles": ["cautious", "looter"], "maps": ["yard"], "seeds": [1, 7],
             "tuning_keys": {"keys": {"speed": {"type": "float", "min": 1., "max": 4.},
                                       "count": {"type": "int", "min": 1, "max": 10}}},
             "metrics": {"groups": [{"profile": "cautious", "map": "yard", "metrics": {
                 "extraction_rate": {"mean": .5, "ci95": [0., 1.], "n": 2},
                 "loot": {"mean": None, "ci95": None, "n": 0}}}]},
             "holdout": None, "anomalies": {"failures": 1},
             "run": {"command": ["python", "-m", "examples.tinyraid", "--seed", "1"], "cwd": None},
             "input_sources": ["run.json", "metrics.json"], "approved_targets": None}
    value["approved_targets"] = generate("targets", value)
    return value


class PlannerSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.context = context()

    def test_five_mock_roundtrips_are_deterministic_and_do_not_mutate_context(self):
        original = deepcopy(self.context)
        for step in STEPS:
            with self.subTest(step=step):
                result = generate(step, self.context)
                self.assertEqual(result, generate(step, self.context))
                validate_output(step, result)
                self.assertEqual(from_wire(step, to_wire(step, result)), result)
                self.assertEqual(normalize(step, result, self.context), (result, []))
        self.assertEqual(self.context, original)

    def test_clipping_both_endpoints_and_untrusted_receipts(self):
        draft = generate("revise", self.context)
        draft["search_ranges"] = {"count": {"min": -10, "max": -1}, "speed": {"min": 2., "max": 10.}}
        draft["clipping"] = [{"key": "fake", "scope": "fake", "requested_min": 1,
                              "requested_max": 2, "applied_min": 1, "applied_max": 2}]
        original = deepcopy(draft)
        result, receipts = normalize("revise", draft, self.context)
        self.assertEqual(result["search_ranges"], {"count": {"min": 1, "max": 1},
                                                  "speed": {"min": 2., "max": 4.}})
        self.assertEqual(result["clipping"], receipts)
        self.assertEqual([r["key"] for r in receipts], ["count", "speed"])
        self.assertEqual(receipts[0]["requested_min"], -10)
        self.assertEqual(draft, original)
        result["clipping"][0]["key"] = "changed"
        self.assertEqual(receipts[0]["key"], "count")

    def test_int_ranges_preserve_precision_and_reject_floats_and_inversion(self):
        low = 2 ** 53 + 1
        self.context["tuning_keys"]["keys"]["count"].update(min=low, max=low + 2)
        draft = generate("revise", self.context)
        draft["search_ranges"] = {"count": {"min": low - 1, "max": low + 1}}
        result, receipts = normalize("revise", draft, self.context)
        self.assertEqual(result["search_ranges"]["count"]["min"], low)
        self.assertEqual(receipts[0]["requested_min"], low - 1)
        for bounds in ({"min": float(low), "max": low + 1}, {"min": -1, "max": -2}):
            draft["search_ranges"]["count"] = bounds
            with self.assertRaises(ValueError):
                normalize("revise", draft, self.context)

    def test_unknown_keys_and_nonfinite_values_are_rejected(self):
        for step in ("revise", "campaign"):
            draft = generate(step, self.context)
            draft["search_ranges"]["unknown"] = {"min": 0, "max": 1}
            with self.assertRaisesRegex(ValueError, "unknown tuning key"):
                normalize(step, draft, self.context)
        draft = generate("revise", self.context)
        draft["search_ranges"]["speed"]["max"] = float("inf")
        with self.assertRaises(ValueError):
            normalize("revise", draft, self.context)

    def test_targets_require_known_unique_identities_and_valid_rate_bounds(self):
        for field, value in (("metric", "invented"), ("profile", "invented"),
                             ("map", "invented"), ("min", -.1), ("max", 1.1)):
            draft = generate("targets", self.context)
            draft["targets"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                normalize("targets", draft, self.context)
        draft = generate("targets", self.context)
        draft["targets"].append(deepcopy(draft["targets"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate target"):
            normalize("targets", draft, self.context)

    def test_campaign_requires_approval_and_unique_configured_subsets(self):
        draft = generate("campaign", self.context)
        self.context["approved_targets"] = None
        with self.assertRaisesRegex(ValueError, "approved_targets"):
            normalize("campaign", draft, self.context)
        self.context = context()
        for field, values in (("profiles", ["unknown"]), ("maps", ["unknown"]),
                              ("seeds", [99]), ("seeds", [1, 1])):
            candidate = deepcopy(draft)
            candidate["experiments"][0][field] = values
            with self.subTest(field=field, values=values), self.assertRaises(ValueError):
                normalize("campaign", candidate, self.context)
        draft["experiments"].append(deepcopy(draft["experiments"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate experiment"):
            normalize("campaign", draft, self.context)

    def test_campaign_instruction_keys_match_used_profiles(self):
        draft = generate("campaign", self.context)
        draft["experiments"][0]["profiles"] = ["cautious"]
        with self.assertRaisesRegex(ValueError, "exactly match"):
            normalize("campaign", draft, self.context)
        del draft["profile_instructions"]["looter"]
        normalize("campaign", draft, self.context)

    def test_experiment_ranges_cannot_exceed_global_ranges(self):
        draft = generate("campaign", self.context)
        draft["search_ranges"]["speed"] = {"min": 2, "max": 3}
        with self.assertRaisesRegex(ValueError, "outside campaign"):
            normalize("campaign", draft, self.context)
        draft["experiments"][0]["tuning_ranges"]["speed"] = {"min": 2, "max": 3}
        normalize("campaign", draft, self.context)
        del draft["search_ranges"]["speed"]
        with self.assertRaisesRegex(ValueError, "outside campaign"):
            normalize("campaign", draft, self.context)

    def test_reports_cannot_invent_values_or_claim_unmeasured_results(self):
        original = generate("report", self.context)
        for changes in ({"value": .9}, {"ci95": [1., 0.]}, {"value": None}, {"n": 0}):
            draft = deepcopy(original)
            draft["results"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize("report", draft, self.context)
        draft = deepcopy(original)
        draft["results"][1]["value"] = 0
        with self.assertRaises(ValueError):
            normalize("report", draft, self.context)
        self.context["metrics"] = None
        with self.assertRaisesRegex(ValueError, "observations"):
            normalize("report", original, self.context)
        self.assertEqual(generate("report", self.context)["results"], [])

    def test_unknown_ci_and_holdout_rows_remain_faithful(self):
        self.context["holdout"] = self.context.pop("metrics")
        self.context["holdout"]["groups"][0]["metrics"]["extraction_rate"]["ci95"] = None
        draft = generate("report", self.context)
        self.assertIsNone(draft["results"][0]["ci95"])
        normalize("report", draft, self.context)

    def test_proposals_are_limited_to_known_typed_bounds(self):
        for key, value in (("unknown", 1), ("count", 1.5), ("count", 11)):
            draft = generate("report", self.context)
            draft["proposed_defaults"] = [{"key": key, "baseline": None, "proposed": value,
                                           "reason": "Draft for human review"}]
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                normalize("report", draft, self.context)

    def test_report_baseline_requires_documented_value_or_unknown_null(self):
        for source in ("default", "baseline"):
            with self.subTest(source=source):
                ctx = deepcopy(self.context)
                ctx["tuning_keys"]["keys"]["speed"][source] = 3.
                draft = generate("report", ctx)
                draft["proposed_defaults"] = [{"key": "speed", "baseline": 1.,
                    "proposed": 4., "reason": "Draft for human review"}]
                with self.assertRaises(ValueError):
                    normalize("report", draft, ctx)
                draft["proposed_defaults"][0]["baseline"] = 3.
                result, _ = normalize("report", draft, ctx)
                self.assertEqual(result["proposed_defaults"][0]["baseline"], 3.)
        draft = generate("report", self.context)
        draft["proposed_defaults"] = [{"key": "speed", "baseline": 1.,
            "proposed": 4., "reason": "Draft for human review"}]
        with self.assertRaises(ValueError):
            normalize("report", draft, self.context)
        draft["proposed_defaults"][0]["baseline"] = None
        result, _ = normalize("report", draft, self.context)
        self.assertIsNone(result["proposed_defaults"][0]["baseline"])

    def test_bug_reproduction_and_evidence_cannot_be_fabricated(self):
        original = generate("bug", self.context)
        self.assertIsNone(original["suspected_cause"])
        for field, value in (("command", ["invented"]), ("cwd", "invented")):
            draft = deepcopy(original)
            draft["reproduction"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "recorded run"):
                normalize("bug", draft, self.context)
        draft = deepcopy(original)
        draft["evidence"].append("invented.json")
        with self.assertRaisesRegex(ValueError, "input_sources"):
            normalize("bug", draft, self.context)
        self.context["run"] = None
        with self.assertRaisesRegex(ValueError, "recorded run"):
            generate("bug", self.context)


if __name__ == "__main__":
    unittest.main()
