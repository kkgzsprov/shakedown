"""Configuration and evidence checks for draft planner outputs.

These checks never approve targets, apply tuning, or execute reproduction text.
Only system-computed clipping receipts are retained.
"""

from __future__ import annotations

from copy import deepcopy
import math

from ..aggregate import METRICS
from .schema import validate_output


_RATES = {name for name in METRICS if name.endswith("_rate")} | {"first_contact_by_player"}


def _finite(value):
    return type(value) is int or type(value) is float and math.isfinite(value)


def _identity(row: dict, context: dict) -> None:
    if row["metric"] not in METRICS:
        raise ValueError(f"unknown metric: {row['metric']}")
    if row["profile"] not in context["profiles"]:
        raise ValueError(f"unknown profile: {row['profile']}")
    if row["map"] is not None and row["map"] not in context["maps"]:
        raise ValueError(f"unknown map: {row['map']}")


def _subset(values: list, allowed: list, label: str) -> None:
    if len(set(values)) != len(values) or any(value not in allowed for value in values):
        raise ValueError(f"{label} must be unique configured values")


def _ranges(ranges: dict, context: dict, scope: str, adjustments: list) -> None:
    keys = context["tuning_keys"]["keys"]
    for key, requested in sorted(ranges.items()):
        if key not in keys:
            raise ValueError(f"unknown tuning key: {key}")
        spec = keys[key]
        low, high = requested["min"], requested["max"]
        if spec["type"] == "int" and (type(low) is not int or type(high) is not int):
            raise ValueError(f"{key}: integer tuning endpoints required")
        if low > high:
            raise ValueError(f"{key}: inverted range")
        applied_low = min(spec["max"], max(spec["min"], low))
        applied_high = min(spec["max"], max(spec["min"], high))
        if (low, high) != (applied_low, applied_high):
            adjustments.append({"key": key, "scope": scope,
                                "requested_min": low, "requested_max": high,
                                "applied_min": applied_low, "applied_max": applied_high})
        requested.update(min=applied_low, max=applied_high)


def observed_results(context: dict) -> list[dict]:
    """Copy measured rows without pooling distinct groups or inventing estimates."""
    rows = []
    for source in (context.get("metrics"), context.get("holdout")):
        if source is None:
            continue
        if type(source) is not dict or type(source.get("groups")) is not list:
            raise ValueError("measurement evidence must contain a groups array")
        for group in source["groups"]:
            if (type(group) is not dict or type(group.get("metrics")) is not dict
                    or "profile" not in group or "map" not in group):
                raise ValueError("measurement group has invalid structure")
            for metric, estimate in sorted(group["metrics"].items()):
                if (type(estimate) is not dict or not {"mean", "ci95", "n"} <= estimate.keys()
                        or type(estimate["n"]) is not int or estimate["n"] < 0):
                    raise ValueError("measurement estimate has invalid structure")
                row = {"metric": metric, "profile": group["profile"], "map": group["map"],
                       "value": estimate["mean"], "ci95": deepcopy(estimate["ci95"]),
                       "n": estimate["n"]}
                _identity(row, context)
                if (row["value"] is not None and not _finite(row["value"]) or
                    row["ci95"] is not None and
                    (type(row["ci95"]) is not list or len(row["ci95"]) != 2 or
                     not all(_finite(value) for value in row["ci95"]) or row["ci95"][0] > row["ci95"][1]) or
                    row["n"] == 0 and (row["value"] is not None or row["ci95"] is not None) or
                    row["n"] > 0 and row["value"] is None):
                    raise ValueError("measurement estimate is inconsistent or nonfinite")
                rows.append(row)
    return rows


def normalize(step: str, output: dict, context: dict) -> tuple[dict, list[dict]]:
    """Validate a canonical draft and return a detached, bounded result and receipts."""
    validate_output(step, output)
    result = deepcopy(output)
    adjustments = []
    if step == "targets":
        seen = set()
        for target in result["targets"]:
            _identity(target, context)
            identity = (target["metric"], target["profile"], target["map"])
            if identity in seen:
                raise ValueError("duplicate target metric/profile/map")
            seen.add(identity)
            if target["min"] > target["max"]:
                raise ValueError("inverted target range")
            if target["metric"] in _RATES and not 0 <= target["min"] <= target["max"] <= 1:
                raise ValueError("rate target must be within 0..1")
    elif step == "campaign":
        if context.get("approved_targets") is None:
            raise ValueError("campaign requires approved_targets")
        normalize("targets", context["approved_targets"], context)
        _ranges(result["search_ranges"], context, "search_ranges", adjustments)
        ids, used_profiles = set(), set()
        for experiment in result["experiments"]:
            if experiment["id"] in ids:
                raise ValueError("duplicate experiment id")
            ids.add(experiment["id"])
            for field in ("profiles", "maps", "seeds"):
                _subset(experiment[field], context[field], field)
            used_profiles.update(experiment["profiles"])
            _ranges(experiment["tuning_ranges"], context,
                    f"experiments.{experiment['id']}.tuning_ranges", adjustments)
            for key, bounds in experiment["tuning_ranges"].items():
                global_bounds = result["search_ranges"].get(key)
                if (global_bounds is None or bounds["min"] < global_bounds["min"]
                        or bounds["max"] > global_bounds["max"]):
                    raise ValueError(f"{key}: experiment range outside campaign search_ranges")
        if set(result["profile_instructions"]) != used_profiles:
            raise ValueError("profile_instructions must exactly match used profiles")
    elif step == "revise":
        _ranges(result["search_ranges"], context, "search_ranges", adjustments)
        result["clipping"] = deepcopy(adjustments)
    elif step == "bug":
        run = context.get("run")
        if not run or not run.get("command"):
            raise ValueError("bug requires recorded run command")
        if result["reproduction"] != {"command": run["command"], "cwd": run.get("cwd")}:
            raise ValueError("reproduction must match recorded run command and cwd")
        if any(item not in context.get("input_sources", []) for item in result["evidence"]):
            raise ValueError("bug evidence must reference supplied input_sources")
    elif step == "report":
        observations = observed_results(context)
        for row in result["results"]:
            _identity(row, context)
            if row["n"] == 0 and (row["value"] is not None or row["ci95"] is not None):
                raise ValueError("unmeasured result requires null value and CI")
            if row["n"] > 0 and row["value"] is None:
                raise ValueError("measured result requires value")
            if row["ci95"] is not None and row["ci95"][0] > row["ci95"][1]:
                raise ValueError("inverted result CI")
            if row not in observations:
                raise ValueError("result does not match supplied observations")
        keys = context["tuning_keys"]["keys"]
        for proposal in result["proposed_defaults"]:
            key = proposal["key"]
            if key not in keys:
                raise ValueError(f"unknown tuning key: {key}")
            spec = keys[key]
            known_baseline = spec.get("default", spec.get("baseline"))
            if proposal["baseline"] is not None and proposal["baseline"] != known_baseline:
                raise ValueError(f"{key}: baseline must match the documented default or remain null")
            for field in ("baseline", "proposed"):
                value = proposal[field]
                if value is None:
                    continue
                if spec["type"] == "int" and type(value) is not int:
                    raise ValueError(f"{key}: integer tuning value required")
                if not spec["min"] <= value <= spec["max"]:
                    raise ValueError(f"{key}: proposal outside key bounds")
    validate_output(step, result)
    return result, adjustments
