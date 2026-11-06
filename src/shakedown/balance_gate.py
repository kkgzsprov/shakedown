"""Conservative holdout evidence gate for human-reviewed balance proposals.

This module evaluates supplied measurements only. It neither establishes the
search/holdout split nor authenticates a policy's implementation; the campaign
runner owns those boundaries and records which policies were actually tested.
"""

from __future__ import annotations

import math

from .optimize import target_score
from .orchestrate import digest


def _finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _arguments(scopes, policies, min_runs, min_gain):
    if type(min_runs) is not int or min_runs < 1:
        raise ValueError("min_runs must be a positive integer")
    if not _finite(min_gain) or not 0 < min_gain <= 1:
        raise ValueError("min_gain must be finite and within (0, 1]")
    if (type(policies) not in (list, tuple) or not policies
            or any(type(policy) is not str or not policy for policy in policies)
            or len(set(policies)) != len(policies)):
        raise ValueError("policies must be nonempty unique names")
    if (type(scopes) not in (list, tuple) or not scopes
            or any(type(scope) not in (list, tuple) or len(scope) != 2
                   or any(type(name) is not str or not name for name in scope)
                   for scope in scopes)):
        raise ValueError("scopes must contain profile/map pairs")
    normalized = [tuple(scope) for scope in scopes]
    if len(set(normalized)) != len(normalized):
        raise ValueError("scopes must be unique")
    return normalized, list(policies)


def _oracle(group, min_runs):
    """Return an oracle-rate estimate only when its full denominator is known."""
    runs = group.get("runs")
    estimates = group.get("metrics")
    value = estimates.get("oracle_rate") if type(estimates) is dict else None
    if (type(runs) is not int or runs < min_runs or type(value) is not dict
            or type(value.get("n")) is not int or value["n"] != runs
            or not _finite(value.get("mean")) or not 0 <= value["mean"] <= 1):
        return None
    ci = value.get("ci95")
    if (type(ci) is not list or len(ci) != 2 or not all(_finite(item) for item in ci)
            or not 0 <= ci[0] <= ci[1] <= 1):
        return None
    return value["mean"]


def evaluate_gate(metrics, targets, *, baseline: dict, candidate: dict,
                  scopes: list[tuple[str, str]], policies: list[str],
                  min_runs=5, min_gain=.2) -> dict:
    """Require measured improvement, oracle safety and policy agreement.

    ``passed`` authorizes producing a proposal artifact, never applying it.
    Missing evidence stays ``unmeasured``; observed regressions and invalid
    executions are ``rejected``. Policy agreement covers only the named tested
    policies and is not evidence of agreement with untested human/model play.
    """
    scopes, policies = _arguments(scopes, policies, min_runs, min_gain)
    if type(baseline) is not dict or type(candidate) is not dict:
        raise ValueError("baseline and candidate must be tuning objects")
    hashes = {"baseline": digest(baseline), "candidate": digest(candidate)}
    result = {"v": 1, "status": "unmeasured", "proposed": False,
              "reasons": [], "baseline_score": None, "candidate_score": None,
              "gain_fraction": None, "policy_sensitive": None, "policy_scores": {},
              "checked_policies": policies, "scopes": [list(scope) for scope in scopes],
              "min_runs": min_runs, "min_gain": min_gain,
              "baseline_hash": hashes["baseline"], "candidate_hash": hashes["candidate"],
              "oracle_comparisons": [], "targets": {"baseline": [], "candidate": []}}
    missing, rejected = [], []
    if len(policies) < 2:
        missing.append("policy sensitivity requires at least two distinct tested policies")
    if type(metrics) is not dict or type(metrics.get("groups")) is not list:
        result["reasons"] = [*missing, "measurement evidence must contain groups"]
        return result
    invalid = metrics.get("invalid_runs")
    if type(invalid) is not list:
        missing.append("invalid-run inventory is missing or malformed")
    elif invalid:
        rejected.append("holdout contains invalid executions")

    groups = {}
    for group in metrics["groups"]:
        fields = ("profile", "map", "decider", "tuning_hash")
        if (type(group) is not dict
                or any(type(group.get(field)) is not str or not group[field] for field in fields)):
            missing.append("malformed measurement group identity")
            continue
        key = tuple(group[field] for field in fields)
        groups.setdefault(key, []).append(group)

    for profile, map_name in scopes:
        for policy in policies:
            rates = {}
            for label, tuning_hash in hashes.items():
                matches = groups.get((profile, map_name, policy, tuning_hash), [])
                if len(matches) != 1:
                    missing.append(f"{label} {profile}/{map_name}/{policy}: missing or duplicate group")
                    continue
                group = matches[0]
                rate = _oracle(group, min_runs)
                if rate is None:
                    missing.append(f"{label} {profile}/{map_name}/{policy}: oracle rate or sample count unmeasured")
                    continue
                rates[label] = rate
            if len(rates) == 2:
                comparison = {"profile": profile, "map": map_name, "policy": policy,
                              "baseline": rates["baseline"], "candidate": rates["candidate"],
                              "delta": rates["candidate"] - rates["baseline"]}
                result["oracle_comparisons"].append(comparison)
                if comparison["delta"] > 0:
                    rejected.append(f"oracle rate increased for {profile}/{map_name}/{policy}")

    scores = {}
    for label, tuning_hash in hashes.items():
        score = target_score(metrics, targets, tuning_hash=tuning_hash, scopes=scopes,
                             policies=policies, min_runs=min_runs)
        scores[label] = score
        result["targets"][label] = score["targets"]
        if score["status"] != "measured" or not _finite(score.get("score")):
            missing.extend(f"{label}: {reason}" for reason in score["reasons"])
            if not score["reasons"]:
                missing.append(f"{label}: target score unmeasured")
        else:
            result[f"{label}_score"] = score["score"]

    preferences = []
    for policy in policies:
        before = scores["baseline"]["per_policy"].get(policy)
        after = scores["candidate"]["per_policy"].get(policy)
        if not _finite(before) or not _finite(after):
            missing.append(f"{policy}: policy target scores unmeasured")
            result["policy_scores"][policy] = {"baseline": None, "candidate": None, "improvement": None}
            continue
        improvement = before - after
        if not _finite(improvement):
            missing.append(f"{policy}: policy target score difference is nonfinite")
            improvement = None
        else:
            preferences.append(1 if improvement > 0 else -1 if improvement < 0 else 0)
        result["policy_scores"][policy] = {"baseline": before, "candidate": after,
                                           "improvement": improvement}
    if len(policies) >= 2 and len(preferences) == len(policies):
        result["policy_sensitive"] = 1 in preferences and -1 in preferences
        if result["policy_sensitive"]:
            rejected.append("tested policies disagree about candidate preference")

    before, after = result["baseline_score"], result["candidate_score"]
    if before is not None and after is not None:
        if before <= 0:
            rejected.append("baseline already satisfies target ranges; no positive gain to propose")
        else:
            # Compute the fractional change without overflowing before-after.
            gain = 1 - after / before
            result["gain_fraction"] = gain if _finite(gain) else None
            if after >= before or after > (1 - min_gain) * before:
                rejected.append("candidate improvement is below min_gain")
    result["reasons"] = list(dict.fromkeys([*rejected, *missing]))
    if rejected:
        result["status"] = "rejected"
    elif not missing:
        result.update(status="passed", proposed=True)
    return result
