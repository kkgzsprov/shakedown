"""Evidence-based batch metrics and same-seed, paired bootstrap comparisons.

Each metric is a mean of per-run observations, with a percentile bootstrap
95% interval. ``n`` is the number of measured runs, not the cell size. A null
mean/interval and n=0 means that the adapter supplied no usable observation.
Latency metrics are means of each run's latency percentile, not pooled calls.
Oracle stops remain valid observations for oracle rate; their survival and
extraction outcomes are censored. Invalid executions never enter comparisons.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import itertools
import json
import math
from pathlib import Path
import random

from .protocol import SOURCES, canonical_json


METRICS = (
    "survival_rate", "extraction_rate", "oracle_rate", "session_seconds",
    "time_to_first_contact_seconds", "deaths_per_contact", "shots", "heals",
    "loot", "first_contact_by_player", "hits_from_unseen_shooters",
    "decisions", "latency_ms_p50", "latency_ms_p95", "fallback_rate",
    "dropped_decision_rate", "longest_repeated_tactic", "cost_usd",
)


def _reject_constant(value):
    raise ValueError(f"nonfinite JSON value: {value}")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def _finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _number(value):
    return float(value) if _finite(value) else None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low, high = math.floor(index), math.ceil(index)
    weight = index - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _mean(values: list[float]) -> float:
    # Dividing before summation avoids an overflowing intermediate sum for
    # finite observations near the largest representable float.
    return math.fsum(value / len(values) for value in values)


def _estimate(values: list[float], samples: int, seed: int, label: str) -> dict:
    if not values:
        return {"mean": None, "ci95": None, "n": 0}
    mean = _mean(values)
    if len(set(values)) == 1:
        interval = [mean, mean]
    else:
        digest = hashlib.sha256(f"{seed}:{label}".encode("utf-8")).digest()
        rng = random.Random(int.from_bytes(digest, "big"))
        bootstrap = [_mean(rng.choices(values, k=len(values))) for _ in range(samples)]
        interval = [_percentile(bootstrap, .025), _percentile(bootstrap, .975)]
    return {"mean": mean, "ci95": interval, "n": len(values)}


def _validate_metadata(meta: dict) -> list[str]:
    reasons = []
    if (type(meta.get("v")) is not int or meta["v"] != 1
            or any(type(meta.get(key)) is not str for key in ("id", "map", "profile", "decider", "tuning_hash"))
            or type(meta.get("seed")) is not int or type(meta.get("tuning")) is not dict
            or type(meta.get("valid")) is not bool
            or type(meta.get("wall_timeout")) is not bool
            or type(meta.get("invalid_reasons")) is not list
            or any(type(reason) is not str for reason in meta.get("invalid_reasons", []))):
        return ["invalid_run_metadata"]
    if meta.get("status") not in {"completed", "finding", "error"}:
        reasons.append("unfinished_run")
    elif meta["status"] == "error":
        reasons.append("execution_error")
    codes = meta.get("exit_codes", {"ok": 0, "contract": 2, "oracle": 4, "divergence": 5})
    if (type(codes) is not dict or set(codes) != {"ok", "contract", "oracle", "divergence"}
            or any(type(code) is not int for code in codes.values()) or len(set(codes.values())) != 4):
        return ["invalid_exit_code_mapping"]
    if meta.get("exit_code") not in (codes["ok"], codes["oracle"]) or type(meta.get("exit_code")) is not int:
        reasons.append("unsuccessful_execution")
    if meta["wall_timeout"]:
        reasons.append("wall_timeout")
    reasons.extend(meta["invalid_reasons"])
    if not meta["valid"] and not meta["invalid_reasons"]:
        reasons.append("run_marked_invalid")
    if hashlib.sha256(canonical_json(meta["tuning"])).hexdigest() != meta["tuning_hash"]:
        reasons.append("tuning_hash_mismatch")
    return reasons


def _read_telemetry(path: Path, meta: dict) -> list[dict]:
    rows = []
    last_step, last_time = -1, -1.0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line, parse_constant=_reject_constant)
            if (type(row) is not dict or type(row.get("type")) is not str
                    or type(row.get("step")) is not int or row["step"] < 0 or row["step"] < last_step
                    or not _finite(row.get("t")) or row["t"] < 0 or row["t"] < last_time):
                raise ValueError("invalid or nonmonotonic telemetry event")
            rows.append(row)
            last_step, last_time = row["step"], row["t"]
    if (not rows or rows[0]["type"] != "run_begin" or rows[-1]["type"] != "run_end"
            or sum(row["type"] == "run_begin" for row in rows) != 1
            or sum(row["type"] == "run_end" for row in rows) != 1):
        raise ValueError("telemetry must contain one opening and one final event")
    end = rows[-1]
    if end.get("outcome") not in {"death", "extract", "timeout", "oracle"}:
        raise ValueError("invalid telemetry outcome")
    oracle_code = meta.get("exit_codes", {"oracle": 4})["oracle"]
    if (meta["exit_code"] == oracle_code) != (end["outcome"] == "oracle"):
        raise ValueError("exit code and telemetry outcome disagree")
    if end["outcome"] == "oracle" and not any(
        row["type"] == "oracle" and row.get("oracle") == end.get("oracle") for row in rows
    ):
        raise ValueError("oracle outcome has no matching oracle event")
    begin = rows[0]
    if ("seed" in begin and begin["seed"] != meta["seed"]) or (
        "map" in begin and begin["map"] != meta["map"]
    ):
        raise ValueError("telemetry startup differs from run metadata")
    counts = end.get("counts", {})
    if type(counts) is not dict or any(not _finite(value) or value < 0 for value in counts.values()):
        raise ValueError("invalid telemetry counters")
    if "loot" in end and (not _finite(end["loot"]) or end["loot"] < 0):
        raise ValueError("invalid loot value")
    return rows


def _read_summary(path: Path, meta: dict, end: dict) -> dict:
    if not path.exists():
        return {}
    summary = _load(path)
    if type(summary) is not dict or summary.get("valid") is not True or summary.get("invalid_reasons"):
        raise ValueError("bridge summary is invalid")
    if (type(summary.get("exit_code")) is not int or summary["exit_code"] != meta["exit_code"]
            or summary.get("wall_timeout") is not False):
        raise ValueError("bridge summary execution disagrees")
    bridge_end = summary.get("end")
    if type(bridge_end) is not dict or any(bridge_end.get(key) != end.get(key) for key in ("outcome", "step")):
        raise ValueError("bridge summary end disagrees")
    rate = summary.get("fallback_rate")
    if not _finite(rate) or not 0 <= rate <= .10:
        raise ValueError("invalid or excessive fallback rate")
    for key in ("latency_ms_p50", "latency_ms_p95", "cost_usd"):
        if summary.get(key) is not None and (not _finite(summary[key]) or summary[key] < 0):
            raise ValueError(f"invalid bridge metric: {key}")
    if type(summary.get("decisions")) is not int or summary["decisions"] < 0:
        raise ValueError("invalid bridge decision count")
    return summary


def _read_tape(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    rows = [json.loads(line, parse_constant=_reject_constant)
            for line in path.read_text(encoding="utf-8").splitlines()]
    if any(type(row) is not dict or type(row.get("seq")) is not int
           or not _finite(row.get("latency_ms")) or row["latency_ms"] < 0
           or type(row.get("choice")) is not str or row.get("source") not in SOURCES
           for row in rows):
        raise ValueError("invalid decision tape")
    if any(left["seq"] >= right["seq"] for left, right in zip(rows, rows[1:])):
        raise ValueError("nonmonotonic decision tape")
    return rows


def _observations(meta: dict, rows: list[dict], summary: dict, tape: list[dict] | None) -> dict:
    end = rows[-1]
    outcome = end["outcome"]
    kinds = Counter(row["type"] for row in rows)
    contacts = [row for row in rows if row["type"] == "contact"]
    decisions = [row for row in rows if row["type"] == "decision"]
    if any(type(row.get("seq")) is not int or type(row.get("choice")) is not str for row in decisions):
        raise ValueError("invalid decision telemetry")
    selected = [row for row in decisions if row.get("status", "selected") == "selected"]
    if len({row["seq"] for row in selected}) != len(selected):
        raise ValueError("duplicate decision selection")
    statuses = defaultdict(set)
    names = {}
    for row in decisions:
        seq, status = row["seq"], row.get("status", "selected")
        if status not in {"selected", "applied", "dropped"} or status in statuses[seq]:
            raise ValueError("invalid or duplicate decision receipt")
        statuses[seq].add(status)
        if {"applied", "dropped"} <= statuses[seq] or names.get(seq, row["choice"]) != row["choice"]:
            raise ValueError("conflicting decision receipts")
        names[seq] = row["choice"]
    # Adapters may emit one final receipt rather than a separate selection event.
    unique = {row["seq"]: row for row in decisions}
    choices = selected or list(unique.values())
    count = len(unique)
    if tape is not None and ({row["seq"] for row in tape} != set(unique)
                             or any(row["choice"] != names[row["seq"]] for row in tape)):
        raise ValueError("decision tape and telemetry disagree")
    if summary and summary.get("decisions") != count:
        raise ValueError("bridge decision count disagrees")
    longest = streak = 0
    last = None
    for choice in choices:
        streak = streak + 1 if choice["choice"] == last else 1
        longest = max(longest, streak)
        last = choice["choice"]
    latencies = [row["latency_ms"] for row in tape] if tape is not None else []
    ordered = sorted(latencies)

    def latency(percentile):
        if ordered:
            return ordered[math.ceil(len(ordered) * percentile / 100) - 1]
        return _number(summary.get(f"latency_ms_p{percentile}"))

    fallback = _number(summary.get("fallback_rate"))
    if fallback is None and tape:
        fallback = sum(row["source"] == "fallback" for row in tape) / len(tape)
    if fallback is None and meta["decider"] == "rule":
        fallback = 0.0
    if fallback is not None and not 0 <= fallback <= .10:
        raise ValueError("excessive fallback rate")
    counts = end.get("counts", {})
    first_observer = end.get("first_observer")
    bullets = [row for row in rows if row["type"] == "damage_taken" and row.get("cause") == "bullet"]
    unseen = (sum(not row["source_seen"] for row in bullets)
              if bullets and all(type(row.get("source_seen")) is bool for row in bullets) else None)
    return {
        "survival_rate": None if outcome == "oracle" else float(outcome != "death"),
        "extraction_rate": None if outcome == "oracle" else float(outcome == "extract"),
        "oracle_rate": float(outcome == "oracle"),
        "session_seconds": rows[-1]["t"] - rows[0]["t"],
        "time_to_first_contact_seconds": contacts[0]["t"] - rows[0]["t"] if contacts else None,
        "deaths_per_contact": float(outcome == "death") / len(contacts) if contacts else None,
        "shots": counts.get("shots", kinds["shot"] or None),
        "heals": counts.get("heals", kinds["heal"] or None),
        "loot": _number(end.get("loot", counts.get("loot"))),
        "first_contact_by_player": float(first_observer == "player") if first_observer in {"player", "guard"} else None,
        "hits_from_unseen_shooters": unseen,
        "decisions": count if decisions or summary else None,
        "latency_ms_p50": latency(50), "latency_ms_p95": latency(95),
        "fallback_rate": fallback,
        "dropped_decision_rate": sum(row.get("status") == "dropped" for row in decisions) / count if count else None,
        "longest_repeated_tactic": longest if decisions or summary else None,
        "cost_usd": _number(summary.get("cost_usd", 0 if meta["decider"] in {"rule", "random", "mock", "tape"} else None)),
    }


def _read_run(folder: Path) -> tuple[dict | None, dict | None, list[str]]:
    try:
        meta = _load(folder / "run.json")
        if type(meta) is not dict:
            raise ValueError("run metadata must be an object")
        reasons = _validate_metadata(meta)
        if reasons:
            return meta, None, reasons
        rows = _read_telemetry(folder / "telemetry.jsonl", meta)
        summary = _read_summary(folder / "bridge_summary.json", meta, rows[-1])
        tape = _read_tape(folder / "decision_tape.jsonl")
        return meta, _observations(meta, rows, summary, tape), []
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        return None, None, [f"invalid_evidence: {error}"]


def _inventory(batch_dir: Path) -> tuple[dict[str, dict] | None, str]:
    """The submission inventory survives even when whole run folders disappear."""
    path = batch_dir / "batch.json"
    if not path.exists():
        return None, "directories"
    try:
        manifest = _load(path)
        if (type(manifest) is not dict or type(manifest.get("v")) is not int
                or manifest["v"] != 1 or type(manifest.get("total_runs")) is not int
                or manifest["total_runs"] < 0):
            raise ValueError("invalid manifest version or total_runs")
        source = "planned_runs" if "planned_runs" in manifest else "runs"
        planned = manifest.get(source)
        if type(planned) is not list or len(planned) != manifest["total_runs"]:
            raise ValueError("inventory length differs from total_runs")
        inventory = {}
        for item in planned:
            if (type(item) is not dict or type(item.get("seed")) is not int
                    or any(type(item.get(key)) is not str or not item[key]
                           for key in ("id", "map", "profile", "decider", "tuning_hash"))):
                raise ValueError("invalid planned run descriptor")
            run_id = item["id"]
            if (run_id in {".", ".."} or any(char in run_id for char in "/\\:")
                    or any(ord(char) < 32 for char in run_id) or run_id.endswith((".", " "))):
                raise ValueError("invalid planned run id")
            if run_id in inventory:
                raise ValueError("duplicate planned run id")
            inventory[run_id] = item
        return inventory, source
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        raise ValueError(f"invalid batch inventory: {error}") from error


def aggregate_batch(batch_dir: Path, *, bootstrap_samples: int = 1000, seed: int = 0) -> dict:
    """Aggregate every run directory, recording invalid evidence explicitly.

    Comparisons report right minus left. Pairings use common seed identifiers,
    never list position. Duplicate seeds in a cell are ambiguous and excluded
    from paired inference, while still contributing to that cell's summary.
    """
    if type(bootstrap_samples) is not int or bootstrap_samples < 1 or type(seed) is not int:
        raise ValueError("bootstrap_samples must be positive and seed must be an integer")
    batch_dir = Path(batch_dir)
    inventory, inventory_source = _inventory(batch_dir)
    runs_dir = batch_dir / "runs"
    if not runs_dir.is_dir() and inventory is None:
        raise ValueError("batch has no runs directory")
    cells = defaultdict(list)
    invalid = []
    observed = {folder.name: folder for folder in runs_dir.iterdir() if folder.is_dir()} if runs_dir.is_dir() else {}
    if inventory is not None and observed.keys() - inventory.keys():
        raise ValueError("invalid batch inventory: unplanned run directories: "
                         + ", ".join(sorted(observed.keys() - inventory.keys())))
    run_ids = sorted(inventory if inventory is not None else observed)
    for planned_id in run_ids:
        folder = runs_dir / planned_id
        if planned_id not in observed:
            invalid.append({"id": planned_id, "path": folder.relative_to(batch_dir).as_posix(),
                            "reasons": ["missing_run_directory"]})
            continue
        meta, observations, reasons = _read_run(folder)
        if inventory is not None and meta is not None and any(
            meta.get(key) != inventory[planned_id][key]
            for key in ("id", "seed", "map", "profile", "decider", "tuning_hash")
        ):
            reasons.append("run_differs_from_inventory")
        if reasons:
            run_id = meta.get("id") if meta else None
            invalid.append({"id": planned_id if inventory is not None else
                            (run_id if type(run_id) is str else folder.name),
                            "path": folder.relative_to(batch_dir).as_posix(), "reasons": reasons})
        else:
            key = (meta["profile"], meta["map"], meta["tuning_hash"], meta["decider"])
            cells[key].append((meta["seed"], observations))
    groups, by_id = [], {}
    for index, (key, values) in enumerate(sorted(cells.items()), 1):
        group_id = f"g{index:04d}"
        group = dict(zip(("profile", "map", "tuning_hash", "decider"), key))
        group.update(id=group_id, runs=len(values), metrics={
            metric: _estimate([row[metric] for _, row in values if row[metric] is not None],
                              bootstrap_samples, seed, f"{key}:{metric}") for metric in METRICS})
        groups.append(group)
        by_id[group_id] = values
    comparisons = []
    for left, right in itertools.combinations(groups, 2):
        if left["profile"] != right["profile"] or left["map"] != right["map"]:
            continue
        tuning_changed = left["tuning_hash"] != right["tuning_hash"]
        policy_changed = left["decider"] != right["decider"]
        if tuning_changed == policy_changed:
            continue
        left_counts = Counter(item[0] for item in by_id[left["id"]])
        right_counts = Counter(item[0] for item in by_id[right["id"]])
        left_seeds = {key: row for key, row in by_id[left["id"]] if left_counts[key] == 1}
        right_seeds = {key: row for key, row in by_id[right["id"]] if right_counts[key] == 1}
        paired = sorted(left_seeds.keys() & right_seeds.keys())
        ambiguous = sorted({key for key, count in left_counts.items() if count > 1}
                           | {key for key, count in right_counts.items() if count > 1})
        comparison = {"left": left["id"], "right": right["id"],
                      "axis": "tuning" if tuning_changed else "decider",
                      "paired_seeds": paired, "ambiguous_seeds": ambiguous, "metrics": {}}
        for metric in METRICS:
            differences = [right_seeds[key][metric] - left_seeds[key][metric] for key in paired
                           if left_seeds[key][metric] is not None and right_seeds[key][metric] is not None]
            comparison["metrics"][metric] = _estimate(
                differences, bootstrap_samples, seed, f"{left['id']}:{right['id']}:{metric}")
        comparisons.append(comparison)
    result = {"v": 1, "total_runs": len(run_ids), "inventory_source": inventory_source,
              "valid_runs": sum(group["runs"] for group in groups),
              "invalid_runs": invalid, "groups": groups, "comparisons": comparisons,
              "bootstrap": {"samples": bootstrap_samples, "seed": seed, "confidence": .95,
                            "method": "percentile bootstrap of per-run means",
                            "comparison": "paired right-minus-left differences"}}
    output = batch_dir / "metrics.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    return result
