"""Measured QA signals with Wilson bounds, separate from confirmed bug verdicts.

Rates count runs, not events: repeated explosions in one run do not pretend to
be independent samples. Missing opportunities/instrumentation stay unmeasured.
An optional clean baseline supplies the overall rate; otherwise it is measured
over the submitted batch itself. No signal reads bug switches or expected labels.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path

from .aggregate import _inventory, _read_run, _read_telemetry
from .protocol import canonical_json


DEFAULT_THRESHOLDS = {
    "unwarned_explosion_death": .25,
    "extraction_zone_timeout": .25,
    "cause_share": .60,
    "early_contact": .50,
    "timeout": .80,
    "no_shots": .80,
    "no_loot": .80,
}
GROUP_FIELDS = frozenset({"map", "profile", "decider", "variant", "tuning_hash"})


def _finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def wilson_lower(hits: int, n: int, z: float = 1.96) -> float | None:
    """Lower endpoint of the two-sided 95% Wilson score interval by default."""
    if (type(hits) is not int or type(n) is not int or not 0 <= hits <= n
            or not _finite(z) or z <= 0):
        raise ValueError("Wilson counts must be integers with 0 <= hits <= n and z > 0")
    if n == 0:
        return None
    p = hits / n
    square = z * z
    center = p + square / (2 * n)
    radius = z * math.sqrt(p * (1 - p) / n + square / (4 * n * n))
    return max(0.0, (center - radius) / (1 + square / n))


def _valid_runs(batch_dir: Path) -> tuple[list[tuple[dict, list[dict]]], list[dict], int]:
    inventory, _ = _inventory(batch_dir)
    root = batch_dir / "runs"
    if not root.is_dir() and inventory is None:
        raise ValueError("batch has no runs directory")
    folders = {item.name: item for item in root.iterdir() if item.is_dir()} if root.is_dir() else {}
    if inventory is not None and folders.keys() - inventory.keys():
        raise ValueError("unplanned run directories in anomaly batch")
    ids = sorted(inventory if inventory is not None else folders)
    valid, invalid = [], []
    for run_id in ids:
        if run_id not in folders:
            invalid.append({"id": run_id, "reasons": ["missing_run_directory"]})
            continue
        meta, _, reasons = _read_run(folders[run_id])
        if inventory is not None and meta is not None and any(
            meta.get(key) != inventory[run_id][key]
            for key in ("id", "seed", "map", "profile", "decider", "tuning_hash")
        ):
            reasons.append("run_differs_from_inventory")
        if not reasons and (type(meta.get("variant", "default")) is not str
                            or not meta.get("variant", "default")):
            reasons.append("invalid_variant")
        if reasons:
            invalid.append({"id": run_id, "reasons": reasons})
        else:
            rows = _read_telemetry(folders[run_id] / "telemetry.jsonl", meta)
            try:
                _validate_signal_evidence(rows)
            except ValueError as error:
                invalid.append({"id": run_id, "reasons": [f"inconsistent_signal_evidence: {error}"]})
            else:
                valid.append((meta, rows))
    return valid, invalid, len(ids)


def _count(end: dict, rows: list[dict], name: str, kind: str | None = None):
    value = end.get("counts", {}).get(name)
    if value is not None:
        return value if _finite(value) and value >= 0 else None
    observed = sum(row["type"] == kind for row in rows) if kind else 0
    return observed if observed else None


def _validate_signal_evidence(rows: list[dict]) -> None:
    """Explicit counters cannot erase hazards or unwarned deaths in the events."""
    end = rows[-1]
    if end["outcome"] == "oracle":
        return
    counts = end.get("counts", {})
    explosions = [row for row in rows if row["type"] == "explosion"]
    explosion_deaths = [row for row in rows
                        if row["type"] == "death" and row.get("cause") == "explosion"]
    unwarned_deaths = sum(row.get("warned") is False for row in explosion_deaths)
    for name in ("explosions", "unwarned_explosion_deaths"):
        if name in counts and (not _finite(counts[name]) or counts[name] < 0
                               or int(counts[name]) != counts[name]):
            raise ValueError(f"{name} must be a nonnegative event count")
    hazard_count = counts.get("explosions")
    unwarned_count = counts.get("unwarned_explosion_deaths")
    if hazard_count is not None and (hazard_count < len(explosions)
                                    or (explosion_deaths and hazard_count == 0)):
        raise ValueError("explosion count contradicts observed hazard events")
    if unwarned_count is not None and unwarned_count < unwarned_deaths:
        raise ValueError("unwarned death count contradicts observed unwarned deaths")
    if (hazard_count is not None and unwarned_count is not None
            and unwarned_count > hazard_count):
        raise ValueError("unwarned deaths exceed explosion opportunities")


def _observations(rows: list[dict], causes: list[str]) -> dict[str, bool | None]:
    _validate_signal_evidence(rows)
    end = rows[-1]
    values = {name: None for name in DEFAULT_THRESHOLDS if name != "cause_share"}
    values.update({f"cause_share:{cause}": None for cause in causes})
    if end["outcome"] == "oracle":
        return values  # An oracle-censored session is not ordinary behavioral evidence.

    values["timeout"] = end["outcome"] == "timeout"
    contacts = [row for row in rows if row["type"] == "contact"]
    contact_count = _count(end, rows, "contacts", "contact")
    if contacts:
        values["early_contact"] = contacts[0]["t"] - rows[0]["t"] < 2
    elif contact_count == 0:
        values["early_contact"] = False
    if contact_count is not None and contact_count > 0:
        shots = _count(end, rows, "shots", "shot")
        if shots is not None:
            values["no_shots"] = shots == 0
    opportunities = _count(end, rows, "loot_opportunities")
    loot = end.get("loot", end.get("counts", {}).get("loot"))
    if opportunities is not None and opportunities > 0 and _finite(loot) and loot >= 0:
        values["no_loot"] = loot == 0

    deaths = [row for row in rows if row["type"] == "death"]
    cause = deaths[-1].get("cause") if deaths else None
    if end["outcome"] == "death" and type(cause) is str and cause:
        for name in causes:
            values[f"cause_share:{name}"] = cause == name

    explosions = [row for row in rows if row["type"] == "explosion"]
    explosion_deaths = [row for row in deaths if row.get("cause") == "explosion"]
    hazard_count = _count(end, rows, "explosions", "explosion")
    explicit_unwarned = _count(end, rows, "unwarned_explosion_deaths")
    if explicit_unwarned is not None and hazard_count is not None and hazard_count > 0:
        values["unwarned_explosion_death"] = explicit_unwarned > 0
    elif explosion_deaths and all(type(row.get("warned")) is bool for row in explosion_deaths):
        values["unwarned_explosion_death"] = any(not row["warned"] for row in explosion_deaths)
    elif (hazard_count is not None and hazard_count > 0 and not explosion_deaths
          and explosions and all(type(row.get("warned")) is bool for row in explosions)):
        values["unwarned_explosion_death"] = False

    attempts = end.get("extraction_attempts", end.get("counts", {}).get("extraction_attempts"))
    zone_seconds = end.get("extract_zone_seconds")
    in_zone = end.get("in_extraction_zone")
    extracted = any(row["type"] == "extract" for row in rows)
    if extracted:
        values["extraction_zone_timeout"] = False
    elif (_finite(attempts) and attempts > 0 and _finite(zone_seconds) and zone_seconds >= 0
          and type(in_zone) is bool):
        values["extraction_zone_timeout"] = in_zone and end["outcome"] == "timeout" and zone_seconds >= 1
    return values


def signal_observation(signal_id: str, metadata: dict, telemetry_rows: list[dict]) -> bool | None:
    """Classify one validated run for reproduction; metadata labels are unused."""
    del metadata
    if signal_id in DEFAULT_THRESHOLDS and signal_id != "cause_share":
        causes = []
    elif isinstance(signal_id, str) and signal_id.startswith("cause_share:") and signal_id[12:]:
        causes = [signal_id[12:]]
    else:
        raise ValueError("unknown anomaly signal")
    if (not telemetry_rows or any(type(row) is not dict or type(row.get("type")) is not str
                                  for row in telemetry_rows)
            or telemetry_rows[-1]["type"] != "run_end"):
        raise ValueError("signal requires a complete validated run")
    return _observations(telemetry_rows, causes)[signal_id]


def _markdown(report: dict) -> str:
    lines = ["# Anomalies", "", "Flagged groups are places to investigate, not confirmed bugs.", "",
             f"Submitted runs: {report['total_runs']}; valid evidence: {report['valid_runs']}; "
             f"invalid evidence: {len(report['invalid_runs'])}.", "",
             "## Flagged groups", ""]
    findings = report["findings"]
    if not findings:
        lines.append("No group met the configured signal threshold, sample minimum, and baseline comparison.")
    else:
        lines += ["| Signal | Group | Hits / opportunities | Wilson lower | Overall rate | Evidence |",
                  "|---|---|---:|---:|---:|---|"]
        for row in findings:
            group = ", ".join(f"{key}={value}" for key, value in row["group"].items()) or "all runs"
            group = group.replace("|", "\\|").replace("\n", " ")
            evidence = ", ".join(f"[{run_id}](runs/{run_id}/telemetry.jsonl)" for run_id in row["run_ids"][:8])
            lines.append(f"| {row['signal']} | {group} | {row['hits']} / {row['n']} | "
                         f"{row['wilson_lower']:.3f} | {row['overall_rate']:.3f} | {evidence} |")
    lines += ["", "## Measurement limits", "",
              "Each opportunity is one run. Missing instrumentation or baseline opportunities are unmeasured, "
              "not zero. Oracle-stopped runs are censored. A group must have at least "
              f"{report['min_runs']} opportunities, and its Wilson lower bound must exceed both the "
              "configured threshold and the measured overall rate.", "",
              f"Baseline source: {report['baseline']['source']}. "
              "Signals use first-contact <2 seconds and extraction timeout with at least 1 second "
              "of zone dwell after an extraction attempt and explicit final occupancy inside the zone. "
              "Counters contradicting observed unwarned deaths are invalid evidence.", ""]
    return "\n".join(lines)


def analyze_batch(batch_dir: Path, *, min_runs: int = 5, thresholds: dict | None = None,
                  group_by: tuple[str, ...] = ("map", "profile", "variant"),
                  baseline_dir: Path | None = None) -> dict:
    """Write versioned signal evidence and an investigation-first Markdown report."""
    if type(min_runs) is not int or min_runs < 1:
        raise ValueError("min_runs must be a positive integer")
    if (type(group_by) not in (tuple, list) or any(type(key) is not str or key not in GROUP_FIELDS for key in group_by)
            or len(set(group_by)) != len(group_by)):
        raise ValueError("group_by must contain unique supported metadata fields")
    if thresholds is not None and (type(thresholds) is not dict or set(thresholds) - DEFAULT_THRESHOLDS.keys()):
        raise ValueError("unknown anomaly thresholds")
    settings = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if any(not _finite(value) or not 0 <= value <= 1 for value in settings.values()):
        raise ValueError("anomaly thresholds must be finite rates from zero to one")
    batch_dir = Path(batch_dir).resolve()
    runs, invalid, total = _valid_runs(batch_dir)
    if baseline_dir is None:
        baseline_runs, baseline_invalid, baseline_total = runs, invalid, total
        source = "batch"
    else:
        baseline_dir = Path(baseline_dir).resolve()
        if baseline_dir == batch_dir:
            raise ValueError("an explicit baseline must be a separate batch")
        baseline_runs, baseline_invalid, baseline_total = _valid_runs(baseline_dir)
        source = str(baseline_dir)
    causes = sorted({row.get("cause") for _, rows in [*runs, *baseline_runs] for row in rows
                     if row["type"] == "death" and type(row.get("cause")) is str and row["cause"]})
    measured = [(meta, _observations(rows, causes)) for meta, rows in runs]
    baseline = [_observations(rows, causes) for _, rows in baseline_runs]
    names = [name for name in DEFAULT_THRESHOLDS if name != "cause_share"] + [f"cause_share:{cause}" for cause in causes]
    groups = defaultdict(list)
    for meta, observations in measured:
        key = tuple(meta.get(field, "default") for field in group_by)
        groups[key].append((meta["id"], observations))
    signals, findings = [], []
    for key, observations in sorted(groups.items()):
        group = dict(zip(group_by, key))
        for name in names:
            values = [(run_id, row[name]) for run_id, row in observations if row[name] is not None]
            baseline_values = [row[name] for row in baseline if row[name] is not None]
            n, hits = len(values), sum(value for _, value in values)
            baseline_n, baseline_hits = len(baseline_values), sum(baseline_values)
            rate = hits / n if n else None
            overall = baseline_hits / baseline_n if baseline_n else None
            lower = wilson_lower(hits, n)
            threshold = settings[name.split(":", 1)[0]]
            if not n or not baseline_n:
                status, reason = "unmeasured", "missing_opportunities"
            elif n < min_runs:
                status, reason = "unmeasured", "insufficient_runs"
            elif lower > threshold and lower > overall:
                status, reason = "flagged", "above_threshold_and_baseline"
            else:
                status, reason = "clear", "not_above_threshold_and_baseline"
            support = [run_id for run_id, value in values if value]
            row = {"id": name, "group": group, "hits": hits, "n": n, "rate": rate,
                   "wilson_lower": lower, "overall_rate": overall, "baseline_hits": baseline_hits,
                   "baseline_n": baseline_n, "threshold": threshold, "status": status,
                   "reason": reason, "run_ids": support,
                   "opportunity_run_ids": [run_id for run_id, _ in values]}
            signals.append(row)
            if status == "flagged":
                identity = hashlib.sha256(canonical_json({"signal": name, "group": group})).hexdigest()[:16]
                findings.append({**row, "id": identity, "mechanism": "anomaly", "signal": name})
    report = {"v": 1, "total_runs": total, "valid_runs": len(runs), "invalid_runs": invalid,
              "min_runs": min_runs, "z": 1.96, "group_by": list(group_by), "thresholds": settings,
              "baseline": {"source": source, "total_runs": baseline_total,
                           "valid_runs": len(baseline_runs), "invalid_runs": baseline_invalid},
              "signals": signals, "findings": findings}
    (batch_dir / "anomalies.json").write_bytes(canonical_json(report) + b"\n")
    (batch_dir / "anomalies.md").write_text(_markdown(report), encoding="utf-8")
    return report
