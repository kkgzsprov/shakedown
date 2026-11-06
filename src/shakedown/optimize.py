"""Deterministic, bounded tuning search and evidence-only target distances.

Search never mutates defaults or evaluates the game. Grid coordinates are
decoded lazily, so a large Cartesian search space is not materialized. Missing
or ambiguous measurement cells remain unmeasured rather than scoring as zero.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import math
import random

from .aggregate import METRICS
from .protocol import canonical_json


def _finite(value):
    return type(value) is int or type(value) is float and math.isfinite(value)


def _value(value, spec, key):
    if not _finite(value) or spec["type"] == "int" and type(value) is not int:
        raise ValueError(f"{key}: invalid numeric tuning value")
    if not spec["min"] <= value <= spec["max"]:
        raise ValueError(f"{key}: tuning value outside key bounds")
    if spec["type"] == "float":
        try:
            value = float(value)
        except OverflowError as error:
            raise ValueError(f"{key}: floating tuning value overflow") from error
        if not math.isfinite(value):
            raise ValueError(f"{key}: nonfinite tuning value")
    return value


def _keys(key_table):
    if type(key_table) is not dict or type(key_table.get("keys")) is not dict:
        raise ValueError("tuning key table requires a keys object")
    keys = key_table["keys"]
    for key, spec in keys.items():
        if (type(key) is not str or not key or type(spec) is not dict
                or type(spec.get("type")) is not str or spec["type"] not in {"int", "float"}
                or not _finite(spec.get("min")) or not _finite(spec.get("max"))
                or spec["min"] > spec["max"]):
            raise ValueError("invalid tuning key specification")
        if spec["type"] == "int" and any(type(spec[field]) is not int for field in ("min", "max")):
            raise ValueError(f"{key}: integer bounds required")
        if spec["type"] == "float":
            _value(spec["min"], spec, key)
            _value(spec["max"], spec, key)
    return keys


def defaults(key_table) -> dict:
    """Return validated explicit defaults for every documented tuning key."""
    values = {}
    for key, spec in sorted(_keys(key_table).items()):
        if "default" not in spec and "baseline" not in spec:
            raise ValueError(f"{key}: documented default or baseline is required")
        values[key] = _value(spec.get("default", spec.get("baseline")), spec, key)
    return values


def _hash(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _baseline(key_table, baseline):
    keys = _keys(key_table)
    values = defaults(key_table)
    if baseline is not None:
        if type(baseline) is not dict or baseline.keys() - keys.keys():
            raise ValueError("baseline contains unknown tuning keys")
        values.update({key: _value(value, keys[key], key) for key, value in baseline.items()})
    return values


def candidates(key_table, ranges, *, method="random", count=4, seed=0,
               grid_points=3, baseline=None, exclude=()) -> list[dict]:
    """Return distinct full tunings, excluding the baseline and supplied hashes.

    Exhausted or numerically duplicate spaces may return fewer than ``count``.
    Both strategies have a bounded draw budget; grid search visits coordinates
    in sorted-key mixed-radix order, with the last key changing fastest.
    """
    keys = _keys(key_table)
    base = _baseline(key_table, baseline)
    if type(method) is not str or method not in {"random", "grid"}:
        raise ValueError("optimizer method must be random or grid")
    if type(count) is not int or count < 0 or type(seed) is not int:
        raise ValueError("candidate count must be nonnegative and seed an integer")
    if type(grid_points) is not int or grid_points < 2:
        raise ValueError("grid_points must be an integer of at least two")
    if type(ranges) is not dict or ranges.keys() - keys.keys():
        raise ValueError("search ranges contain unknown tuning keys")
    dimensions = []
    for key, bounds in sorted(ranges.items()):
        if type(bounds) is not dict or set(bounds) != {"min", "max"}:
            raise ValueError(f"{key}: range requires min and max")
        low = _value(bounds["min"], keys[key], key)
        high = _value(bounds["max"], keys[key], key)
        if low > high:
            raise ValueError(f"{key}: inverted search range")
        size = min(grid_points, high - low + 1) if keys[key]["type"] == "int" else grid_points
        dimensions.append((key, low, high, 1 if low == high else size))
    if type(exclude) not in (tuple, list, set, frozenset):
        raise ValueError("excluded candidates must be an array or set")
    excluded = {_hash(base)}
    for item in exclude:
        if type(item) is dict:
            excluded.add(_hash(_baseline(key_table, item)))
        elif (type(item) is str and len(item) == 64
              and all(character in "0123456789abcdef" for character in item)):
            excluded.add(item)
        else:
            raise ValueError("excluded candidates must be tunings or SHA-256 hashes")
    if not dimensions or count == 0:
        return []
    result, seen = [], set()
    draw_limit = max(1000, count * 100 + len(excluded) * 10)
    grid_size = math.prod(dimension[3] for dimension in dimensions)
    discrete_size = (math.prod(high - low + 1 for key, low, high, _ in dimensions)
                     if all(keys[key]["type"] == "int" or low == high
                            for key, low, high, _ in dimensions) else None)
    rng = random.Random(seed)
    for index in range(min(grid_size, draw_limit) if method == "grid" else draw_limit):
        value, coordinate = dict(base), index
        for key, low, high, size in reversed(dimensions):
            if method == "random":
                selected = rng.randint(low, high) if keys[key]["type"] == "int" else (
                    low * (1 - (fraction := rng.random())) + high * fraction)
            else:
                offset, coordinate = coordinate % size, coordinate // size
                if size == 1:
                    selected = low
                elif keys[key]["type"] == "int":
                    selected = low + (high - low) * offset // (size - 1)
                else:
                    fraction = offset / (size - 1)
                    selected = low * (1 - fraction) + high * fraction
            value[key] = _value(selected, keys[key], key)
        identity = _hash(value)
        if identity not in seen:
            seen.add(identity)
            if identity not in excluded:
                result.append(value)
                if len(result) == count:
                    break
        if method == "random" and discrete_size is not None and len(seen) >= discrete_size:
            break
    return result


def _target_cells(targets, scopes):
    if type(targets) is dict:
        targets = targets.get("targets")
    if type(targets) is not list or not targets:
        raise ValueError("target scoring requires a nonempty targets array")
    cells, identities = [], set()
    for index, target in enumerate(targets):
        if (type(target) is not dict or target.get("metric") not in METRICS
                or type(target.get("profile")) is not str
                or target.get("map") is not None and type(target["map"]) is not str
                or not _finite(target.get("min")) or not _finite(target.get("max"))
                or target["min"] > target["max"]):
            raise ValueError("invalid target definition")
        identity = (target["metric"], target["profile"], target.get("map"))
        if identity in identities:
            raise ValueError("duplicate target definition")
        identities.add(identity)
        covered = [(profile, map_name) for profile, map_name in scopes
                   if profile == target["profile"] and
                   (target.get("map") is None or map_name == target["map"])]
        if not covered:
            raise ValueError("target is not covered by campaign scopes")
        for profile, map_name in covered:
            cells.append((index, target, profile, map_name))
    return cells


def target_score(metrics, targets, *, tuning_hash, scopes, policies, min_runs=1) -> dict:
    """Sum out-of-range distances only when every requested cell is measured."""
    if (type(scopes) not in (list, tuple) or not scopes
            or any(type(scope) not in (list, tuple) or len(scope) != 2
                   or any(type(part) is not str or not part for part in scope) for scope in scopes)):
        raise ValueError("scopes require profile/map pairs")
    scopes = [tuple(scope) for scope in scopes]
    if len(set(scopes)) != len(scopes):
        raise ValueError("scopes must be unique")
    if (type(policies) not in (list, tuple) or not policies
            or any(type(policy) is not str or not policy for policy in policies)
            or len(set(policies)) != len(policies) or type(min_runs) is not int or min_runs < 1
            or type(tuning_hash) is not str or not tuning_hash):
        raise ValueError("invalid scoring policy, sample count or tuning hash")
    cells = _target_cells(targets, scopes)
    reasons, groups = [], defaultdict(list)
    if type(metrics) is not dict or type(metrics.get("groups")) is not list:
        reasons.append("metrics_groups_missing")
    else:
        for group in metrics["groups"]:
            if type(group) is not dict or type(group.get("tuning_hash")) is not str:
                reasons.append("malformed_measurement_group")
                continue
            if group["tuning_hash"] != tuning_hash:
                continue
            identity = (group.get("profile"), group.get("map"), group.get("decider"))
            if any(type(value) is not str for value in identity):
                reasons.append("malformed_measurement_identity")
                continue
            groups[identity].append(group)
    details, distances = [], {policy: [] for policy in policies}
    failed = {policy: bool(reasons) for policy in policies}
    for index, target, profile, map_name in cells:
        for policy in policies:
            detail = {"target_index": index, "metric": target["metric"], "profile": profile,
                      "map": map_name, "policy": policy, "min": target["min"], "max": target["max"],
                      "value": None, "n": 0, "distance": None}
            matches = groups.get((profile, map_name, policy), [])
            reason = None
            if len(matches) != 1:
                reason = "missing_group" if not matches else "duplicate_group"
            else:
                group = matches[0]
                estimates = group.get("metrics")
                estimate = estimates.get(target["metric"]) if type(estimates) is dict else None
                runs = group.get("runs")
                if (type(estimate) is not dict or type(runs) is not int or runs < min_runs
                        or type(estimate.get("n")) is not int or not min_runs <= estimate["n"] <= runs
                        or not _finite(estimate.get("mean"))):
                    reason = "target_measurement_missing_or_insufficient"
                else:
                    interval = estimate.get("ci95")
                    if (type(interval) is not list or len(interval) != 2
                            or not all(_finite(value) for value in interval) or interval[0] > interval[1]):
                        reason = "target_interval_missing_or_invalid"
                    else:
                        try:
                            distance = max(target["min"] - estimate["mean"],
                                           estimate["mean"] - target["max"], 0)
                        except OverflowError:
                            distance = float("inf")
                        if not _finite(distance):
                            reason = "target_distance_overflow"
                        else:
                            detail.update(value=estimate["mean"], n=estimate["n"], distance=distance)
                            distances[policy].append(distance)
            if reason:
                failed[policy] = True
                detail["reason"] = reason
                reasons.append(f"{reason}:{target['metric']}:{profile}:{map_name}:{policy}")
            details.append(detail)
    per_policy = {}
    for policy in policies:
        try:
            score = sum(distances[policy])
        except OverflowError:
            score = float("inf")
        if not _finite(score):
            failed[policy] = True
            reasons.append(f"target_score_overflow:{policy}")
        per_policy[policy] = None if failed[policy] else score
    measured = not reasons and all(value is not None for value in per_policy.values())
    try:
        score = sum(per_policy.values()) if measured else None
    except OverflowError:
        score = float("inf")
    if measured and not _finite(score):
        reasons.append("target_score_overflow")
        measured, score = False, None
    return {"status": "measured" if measured else "unmeasured", "score": score,
            "per_policy": per_policy, "targets": details, "reasons": list(dict.fromkeys(reasons))}
