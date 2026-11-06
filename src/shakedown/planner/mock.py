"""Deterministic, evidence-only planner drafts for offline transport testing."""

from __future__ import annotations

from copy import deepcopy
import json

from .semantics import normalize, observed_results


def generate(step: str, context: dict) -> dict:
    """Produce a canonical mock draft; this never authorizes or applies changes."""
    ranges = {key: {"min": spec["min"], "max": spec["max"]}
              for key, spec in sorted(context["tuning_keys"]["keys"].items())}
    if step == "targets":
        result = {"schema_version": 1, "targets": [
            {"metric": "extraction_rate", "profile": profile, "map": map_name,
             "min": 0, "max": 1, "rationale": "Illustrative mock draft; requires human review."}
            for profile in context["profiles"] for map_name in (context["maps"] or [None])],
            "notes": "Mock draft only; these targets are not human-approved."}
    elif step == "campaign":
        result = {"schema_version": 1, "hypotheses": ["Measure the configured baseline."],
                  "experiments": [{"id": "baseline", "hypothesis": "Measure before proposing changes.",
                                   "profiles": deepcopy(context["profiles"]),
                                   "maps": deepcopy(context["maps"]), "seeds": deepcopy(context["seeds"]),
                                   "tuning_ranges": deepcopy(ranges)}],
                  "profile_instructions": {profile: "Follow the configured profile and record observations."
                                           for profile in context["profiles"]},
                  "search_ranges": ranges, "batch_size": max(1, min(100000, len(context["seeds"]))),
                  "rounds": 1, "stop_conditions": ["Stop after the baseline round or budget exhaustion."]}
    elif step == "revise":
        result = {"schema_version": 1, "hypotheses": [], "search_ranges": ranges,
                  "decision": "stop", "reason": "Mock does not invent optimization decisions.", "clipping": []}
    elif step == "bug":
        run = context.get("run")
        if not run or not run.get("command"):
            raise ValueError("bug requires recorded run command")
        result = {"schema_version": 1, "title": "Recorded run requires investigation",
                  "reproduction": {"command": deepcopy(run["command"]), "cwd": run.get("cwd")},
                  "expected": "The recorded run satisfies its configured contract and oracle checks.",
                  "actual": "Supplied run evidence requires review; mock makes no causal diagnosis.",
                  "suspected_cause": None, "evidence": deepcopy(context.get("input_sources", []))}
    elif step == "report":
        anomalies = context.get("anomalies")
        result = {"schema_version": 1, "summary": "Mock draft of supplied measurements; requires human review.",
                  "anomalies": ([] if anomalies is None else [anomalies if isinstance(anomalies, str)
                                  else json.dumps(anomalies, sort_keys=True, ensure_ascii=False)]),
                  "results": observed_results(context), "proposed_defaults": [],
                  "limitations": ["Mock does not infer balance verdicts or propose defaults.",
                                  "Null measurements remain unmeasured; no observations are invented."]}
    else:
        raise ValueError(f"unknown planner step: {step}")
    return normalize(step, result, context)[0]
