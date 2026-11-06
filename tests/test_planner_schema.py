"""Structural planner contracts and lossless strict-output map transport."""

from copy import deepcopy
import unittest

import tests  # noqa: F401 - Make source imports work without installation.

from shakedown.planner.schema import (
    STEPS, SchemaError, _check_schema, from_wire, load_schema, to_wire, validate_output,
)


def examples():
    return {
        "targets": {"schema_version": 1, "targets": [{"metric": "survival_rate",
                    "profile": "cautious", "map": None, "min": .4, "max": .6,
                    "rationale": "Human-edited target"}], "notes": "A draft, not approval"},
        "campaign": {"schema_version": 1, "hypotheses": ["Cover may increase survival"],
                     "experiments": [{"id": "e1", "hypothesis": "Compare known speed bounds",
                                      "profiles": ["cautious"], "maps": ["yard"], "seeds": [1, 7],
                                      "tuning_ranges": {"player.speed": {"min": 1, "max": 4}}}],
                     "profile_instructions": {"looter": "Collect loot", "cautious": "엄폐를 먼저 선택"},
                     "search_ranges": {"player.speed": {"min": 1, "max": 4}},
                     "batch_size": 20, "rounds": 2, "stop_conditions": ["Stop at budget"]},
        "revise": {"schema_version": 1, "hypotheses": [], "search_ranges": {},
                   "decision": "stop", "reason": "No further approved budget", "clipping": []},
        "bug": {"schema_version": 1, "title": "Player stuck at wall", "reproduction": {
                "command": ["python", "-m", "examples.tinyraid"], "cwd": None},
                "expected": "Movement progresses", "actual": "No progress for ten seconds",
                "suspected_cause": None, "evidence": ["telemetry.jsonl"]},
        "report": {"schema_version": 1, "summary": "Evidence only", "anomalies": [],
                   "results": [{"metric": "survival_rate", "profile": "cautious", "map": "yard",
                                "value": None, "ci95": None, "n": 0}],
                   "proposed_defaults": [{"key": "player.speed", "baseline": 3.0,
                                          "proposed": 2.5, "reason": "Requires human review"}],
                   "limitations": ["No holdout evidence"]},
    }


class PlannerSchemaTests(unittest.TestCase):
    def test_all_five_canonical_examples_and_wire_roundtrips(self):
        values = examples()
        self.assertEqual(tuple(values), STEPS)
        for step, value in values.items():
            with self.subTest(step=step):
                original = deepcopy(value)
                self.assertIsNone(validate_output(step, value))
                wire = to_wire(step, value)
                self.assertIsNone(validate_output(step, wire, wire=True))
                self.assertEqual(from_wire(step, wire), value)
                self.assertEqual(value, original)

    def test_wire_objects_are_closed_and_require_every_property(self):
        def check(schema):
            kinds = schema["type"] if type(schema["type"]) is list else [schema["type"]]
            if "object" in kinds:
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
                for value in schema["properties"].values():
                    check(value)
            if "items" in schema:
                check(schema["items"])

        for step in STEPS:
            with self.subTest(step=step):
                check(load_schema(step, wire=True))

    def test_campaign_maps_use_sorted_key_value_arrays_including_experiments(self):
        value = examples()["campaign"]
        wire = to_wire("campaign", value)
        self.assertEqual(wire["profile_instructions"], [
            {"key": "cautious", "value": "엄폐를 먼저 선택"},
            {"key": "looter", "value": "Collect loot"},
        ])
        self.assertEqual(wire["search_ranges"], [{"key": "player.speed", "value": {"min": 1, "max": 4}}])
        self.assertEqual(wire["experiments"][0]["tuning_ranges"], wire["search_ranges"])

    def test_duplicate_wire_map_keys_are_not_silently_overwritten(self):
        wire = to_wire("campaign", examples()["campaign"])
        for field in ("profile_instructions", "search_ranges", "experiment"):
            with self.subTest(field=field):
                bad = deepcopy(wire)
                pairs = (bad["experiments"][0]["tuning_ranges"] if field == "experiment" else bad[field])
                pairs.append(deepcopy(pairs[0]))
                with self.assertRaisesRegex(SchemaError, "duplicate map key"):
                    from_wire("campaign", bad)
        revise = to_wire("revise", {**examples()["revise"], "search_ranges": {"x": {"min": 1, "max": 2}}})
        revise["search_ranges"].append(deepcopy(revise["search_ranges"][0]))
        with self.assertRaisesRegex(SchemaError, "duplicate map key"):
            from_wire("revise", revise)

    def test_boolean_is_not_an_integer_or_number(self):
        targets = examples()["targets"]
        for field in ("schema_version", "min", "max"):
            with self.subTest(field=field):
                value = deepcopy(targets)
                (value if field == "schema_version" else value["targets"][0])[field] = True
                with self.assertRaises(SchemaError):
                    validate_output("targets", value)
        campaign = examples()["campaign"]
        campaign["batch_size"] = False
        with self.assertRaises(SchemaError):
            validate_output("campaign", campaign)

    def test_nonfinite_numbers_are_rejected_even_in_nullable_fields(self):
        for number in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(number=number):
                report = examples()["report"]
                report["results"][0]["value"] = number
                with self.assertRaises(SchemaError):
                    validate_output("report", report)
                target = examples()["targets"]
                target["targets"][0]["min"] = number
                with self.assertRaises(SchemaError):
                    validate_output("targets", target)

    def test_unknown_and_missing_fields_fail_at_root_and_nested_levels(self):
        for step, value in examples().items():
            with self.subTest(step=step):
                bad = deepcopy(value)
                bad["approved"] = True
                with self.assertRaisesRegex(SchemaError, "unknown property"):
                    validate_output(step, bad)
                bad = deepcopy(value)
                del bad["schema_version"]
                with self.assertRaisesRegex(SchemaError, "missing properties"):
                    validate_output(step, bad)
        campaign = examples()["campaign"]
        campaign["experiments"][0]["shell"] = "dangerous"
        with self.assertRaisesRegex(SchemaError, "unknown property"):
            validate_output("campaign", campaign)

    def test_profile_instruction_limit_counts_unicode_characters(self):
        value = examples()["campaign"]
        value["profile_instructions"] = {"cautious": "한" * 600}
        validate_output("campaign", value)
        validate_output("campaign", to_wire("campaign", value), wire=True)
        value["profile_instructions"]["cautious"] += "글"
        with self.assertRaisesRegex(SchemaError, "string length"):
            validate_output("campaign", value)

    def test_nullable_fields_accept_null_but_required_fields_stay_required(self):
        report = examples()["report"]
        validate_output("report", report)
        report["results"][0].pop("ci95")
        with self.assertRaises(SchemaError):
            validate_output("report", report)
        bug = examples()["bug"]
        bug["title"] = None
        with self.assertRaises(SchemaError):
            validate_output("bug", bug)

    def test_revision_clipping_receipts_are_required_closed_and_numeric(self):
        revision = examples()["revise"]
        receipt = {"key": "player.speed", "scope": "search_ranges", "requested_min": -1,
                   "requested_max": 8, "applied_min": 1, "applied_max": 5}
        revision["clipping"] = [receipt]
        validate_output("revise", revision)
        self.assertEqual(from_wire("revise", to_wire("revise", revision)), revision)
        for change in ("missing", "extra", "boolean"):
            with self.subTest(change=change):
                invalid = deepcopy(revision)
                if change == "missing":
                    del invalid["clipping"]
                elif change == "extra":
                    invalid["clipping"][0]["trusted"] = True
                else:
                    invalid["clipping"][0]["applied_min"] = False
                with self.assertRaises(SchemaError):
                    validate_output("revise", invalid)

    def test_item_counts_and_numeric_bounds_are_enforced(self):
        campaign = examples()["campaign"]
        for value in (0, 100001, 1.5):
            with self.subTest(value=value):
                campaign["batch_size"] = value
                with self.assertRaises(SchemaError):
                    validate_output("campaign", campaign)
        for interval in ([], [1], [1, 2, 3]):
            report = examples()["report"]
            report["results"][0]["ci95"] = interval
            with self.assertRaises(SchemaError):
                validate_output("report", report)
        target = examples()["targets"]
        target["targets"] = []
        with self.assertRaises(SchemaError):
            validate_output("targets", target)

    def test_empty_map_keys_and_non_json_containers_are_rejected(self):
        value = examples()["campaign"]
        value["search_ranges"] = {"": {"min": 1, "max": 2}}
        with self.assertRaises(SchemaError):
            validate_output("campaign", value)
        value["search_ranges"] = {1: {"min": 1, "max": 2}}
        with self.assertRaises(SchemaError):
            validate_output("campaign", value)
        value = examples()["revise"]
        value["hypotheses"] = ("not a JSON array",)
        with self.assertRaises(SchemaError):
            validate_output("revise", value)

    def test_schema_loader_returns_independent_documents_and_rejects_bad_steps(self):
        first = load_schema("bug")
        first["properties"]["title"]["type"] = "integer"
        self.assertEqual(load_schema("bug")["properties"]["title"]["type"], "string")
        for step in ("../targets", "report.md", "unknown"):
            with self.assertRaises(SchemaError):
                load_schema(step)
        with self.assertRaises(SchemaError):
            load_schema("bug", wire=1)

    def test_unsupported_schema_constraints_cannot_be_silently_ignored(self):
        for schema in (
            {"type": "string", "pattern": "x"},
            {"type": "array", "items": {"type": "string", "format": "email"}},
            {"type": "object", "additionalProperties": "false"},
            {"type": "integer", "minimum": float("nan")},
            {"type": "object", "properties": {}, "required": ["absent"]},
        ):
            with self.subTest(schema=schema), self.assertRaises(SchemaError):
                _check_schema(schema)


if __name__ == "__main__":
    unittest.main()
