"""Execute one explicitly approved, finite campaign of configured candidates."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

from ..orchestrate import run_batch, write_json
from ..protocol import canonical_json


def _candidate_bounds(config, campaign: dict) -> None:
    settings = config.raw.get("tuning", {})
    keys = {}
    if settings.get("keys"):
        path = (config.path.parent / settings["keys"]).resolve()
        keys = json.loads(path.read_text(encoding="utf-8"))["keys"]
    for experiment in campaign["experiments"]:
        ranges = experiment["tuning_ranges"]
        for candidate in config.tuning_candidates:
            if set(candidate) - set(ranges):
                raise ValueError("experiment tuning_ranges must cover configured candidate keys")
            for key, bounds in ranges.items():
                spec = keys[key]
                if key in candidate:
                    value = candidate[key]
                elif "default" in spec:
                    value = spec["default"]
                elif "baseline" in spec:
                    value = spec["baseline"]
                else:
                    raise ValueError(f"{key}: missing candidate value and documented default/baseline")
                if not bounds["min"] <= value <= bounds["max"]:
                    raise ValueError(f"{key}: configured candidate outside approved experiment range")


def run_approved_campaign(config, campaign, approval, output_dir, *, approved=False,
                          targets, targets_approval, max_runs=1000) -> dict:
    """Run approved profile instructions without generating tuning or later rounds."""
    from .engine import approved_campaign

    if approved is not True:
        raise ValueError("explicit campaign execution approval is required")
    if type(max_runs) is not int or max_runs < 1:
        raise ValueError("max_runs must be a positive integer")
    document = approved_campaign(config, campaign, approval, targets, targets_approval)
    if document["rounds"] != 1:
        raise ValueError("M4 campaign execution supports exactly one round")
    _candidate_bounds(config, document)
    plans = []
    total = 0
    for index, experiment in enumerate(document["experiments"], 1):
        seeds = tuple(experiment["seeds"][:document["batch_size"]])
        for profile_index, profile in enumerate(experiment["profiles"], 1):
            derived = replace(config, maps=tuple(experiment["maps"]), profiles=(profile,),
                              seeds=seeds, instructions=document["profile_instructions"][profile])
            count = len(seeds) * len(derived.maps) * len(derived.backends) * len(derived.tuning_candidates)
            total += count
            plans.append((f"e{index:04d}/p{profile_index:04d}", experiment["id"], derived, count))
    if total > max_runs:
        raise ValueError(f"campaign plans {total} runs, exceeding max_runs={max_runs}")
    # Everything above this line is read-only, including both approval checks.
    targets_document = json.loads(Path(targets).read_text(encoding="utf-8"))
    receipt = {"v": 1, "status": "running", "total_runs": total, "runs": [], "batches": [],
               "campaign_sha256": hashlib.sha256(canonical_json(document)).hexdigest(),
               "targets_sha256": hashlib.sha256(canonical_json(targets_document)).hexdigest(),
               "campaign_approval": str(Path(approval).resolve()),
               "targets_approval": str(Path(targets_approval).resolve())}
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt_path = output_dir / "campaign_run.json"
    write_json(receipt_path, receipt)
    try:
        for relative, experiment_id, derived, count in plans:
            batch = {"path": relative, "experiment": experiment_id,
                     "profile": derived.profiles[0], "planned_runs": count, "status": "running"}
            receipt["batches"].append(batch)
            write_json(receipt_path, receipt)
            manifest = run_batch(derived, output_dir / relative)
            batch.update(status=manifest["status"],
                         valid_runs=manifest.get("valid_runs", 0),
                         invalid_runs=manifest.get("invalid_runs", 0))
            receipt["runs"].extend({"batch": relative, **row} for row in manifest["runs"])
            if manifest["status"] != "completed":
                receipt["status"] = manifest["status"]
                break
            if manifest.get("invalid_runs", 0):
                receipt["status"] = "failed"
                break
            write_json(receipt_path, receipt)
        else:
            receipt["status"] = "completed"
    except BaseException as error:
        receipt["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        if receipt["batches"] and receipt["batches"][-1]["status"] == "running":
            receipt["batches"][-1]["status"] = receipt["status"]
        receipt["error"] = f"{type(error).__name__}: {error}"[:500]
        raise
    finally:
        write_json(receipt_path, receipt)
    return receipt
