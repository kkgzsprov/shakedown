"""Seeded coverage sampling with explicit run and wall-clock budgets."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import time

from .aggregate import aggregate_batch
from .config import ProjectConfig
from .orchestrate import digest, run_case, write_json
from .processes import ProcessControl
from .protocol import canonical_json


def _variants(config: ProjectConfig) -> list[dict]:
    settings = config.raw.get("explore", {})
    if type(settings) is not dict or set(settings) - {"variants"}:
        raise ValueError("explore must contain only a variants array")
    variants = settings.get("variants", [{"name": "default", "args": []}])
    if type(variants) is not list or not variants:
        raise ValueError("explore.variants must be a nonempty array")
    names = set()
    for variant in variants:
        if (type(variant) is not dict or set(variant) != {"name", "args"}
                or type(variant["name"]) is not str or not variant["name"].strip()
                or "\x00" in variant["name"] or variant["name"] in names
                or type(variant["args"]) is not list
                or any(type(item) is not str or "\x00" in item for item in variant["args"])
                or (variant["args"] and not variant["args"][0])):
            raise ValueError("each exploration variant needs a unique name and an argument array")
        names.add(variant["name"])
    return variants


def _noise_keys(config: ProjectConfig, count: int) -> dict:
    if not count:
        return {}
    setting = config.raw.get("tuning", {}).get("keys")
    if type(setting) is not str or not setting:
        raise ValueError("tuning noise requires tuning.keys")
    path = config.path.parent / setting
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = data.get("keys") if type(data) is dict else None
    if type(keys) is not dict or not keys:
        raise ValueError("tuning noise requires a nonempty key table")
    for name, spec in keys.items():
        if (type(name) is not str or type(spec) is not dict
                or spec.get("type") not in {"int", "float"}
                or any(type(spec.get(key)) not in (int, float) for key in ("min", "max"))):
            raise ValueError("invalid tuning noise key specification")
        try:
            if not all(math.isfinite(spec[key]) for key in ("min", "max")) or spec["min"] > spec["max"]:
                raise ValueError("invalid tuning noise bounds")
        except OverflowError as error:
            raise ValueError("tuning noise bounds must be finite") from error
        if spec["type"] == "int" and any(type(spec[key]) is not int for key in ("min", "max")):
            raise ValueError("integer tuning noise requires integer bounds")
    return keys


def _sample_plan(config: ProjectConfig, max_runs: int, seed: int,
                 tuning_noise: int, steer: bool, variants: list[dict], keys: dict) -> list[dict]:
    rng = random.Random(seed)
    cells = list(itertools.product(config.maps, config.profiles, config.backends, range(len(variants))))
    if not cells or not config.seeds or not config.tuning_candidates:
        raise ValueError("exploration requires nonempty cells, seed pool, and tuning candidates")
    coverage = [0] * len(cells)
    seed_orders = [rng.sample(list(config.seeds), len(config.seeds)) for _ in cells]
    seed_positions = [0] * len(cells)
    plan = []
    for index in range(max_runs):
        eligible = ([cell for cell, count in enumerate(coverage) if count == min(coverage)]
                    if steer else list(range(len(cells))))
        cell = rng.choice(eligible)
        coverage[cell] += 1
        map_name, profile, decider, variant_index = cells[cell]
        if seed_positions[cell] == len(seed_orders[cell]):
            rng.shuffle(seed_orders[cell])
            seed_positions[cell] = 0
        run_seed = seed_orders[cell][seed_positions[cell]]
        seed_positions[cell] += 1
        variant = variants[variant_index]
        tuning = dict(rng.choice(config.tuning_candidates))
        noisy = []
        if tuning_noise:
            names = rng.sample(sorted(keys), rng.randint(1, min(tuning_noise, len(keys))))
            for name in names:
                spec = keys[name]
                if spec["type"] == "int":
                    tuning[name] = rng.randint(spec["min"], spec["max"])
                else:
                    fraction = rng.random()
                    value = spec["min"] * (1 - fraction) + spec["max"] * fraction
                    tuning[name] = min(spec["max"], max(spec["min"], value))
                noisy.append(name)
        plan.append({"id": f"r{index + 1:06d}", "seed": run_seed,
                     "map": map_name, "profile": profile, "decider": decider,
                     "variant": variant["name"], "extra_args": list(variant["args"]),
                     "tuning": tuning, "tuning_hash": digest(tuning), "seconds": config.duration,
                     "noise_keys": noisy})
    return plan


def _failed_record(config: ProjectConfig, output: Path, spec: dict, reason: str,
                   error: BaseException | None = None) -> dict:
    """Keep a submitted attempt even if execution was interrupted before returning."""
    directory = output / "runs" / spec["id"]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        record = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        if type(record) is not dict or type(record.get("invalid_reasons")) is not list:
            raise ValueError("invalid attempt record")
    except (OSError, ValueError, TypeError):
        record = {"v": 1, **spec, "config_path": str(config.path), "exit_code": None,
                  "exit_codes": dict(config.exit_codes), "wall_timeout": False, "result": None,
                  "invalid_reasons": []}
    record.update(status="error", valid=False,
                  invalid_reasons=list(dict.fromkeys([*record["invalid_reasons"], reason])))
    if error is not None:
        record["error"] = f"{type(error).__name__}: {error}"[:500]
    write_json(directory / "run.json", record)
    return record


def explore(config: ProjectConfig, output_dir: Path, *, max_runs: int = 60,
            deadline_seconds: float | None = None, seed: int = 0,
            tuning_noise: int = 0, steer: bool = True) -> dict:
    """Run a reproducible plan until its attempt budget or deadline is reached.

    The complete sampled plan is preserved separately from the attempted
    inventory. Runs not started before a deadline are skipped, not game failures.
    Sequential execution ensures no queued child begins after its deadline.
    """
    if type(max_runs) is not int or max_runs < 0:
        raise ValueError("max_runs must be a nonnegative integer")
    if type(seed) is not int or type(steer) is not bool:
        raise ValueError("sampler seed must be an integer and steer must be boolean")
    if type(tuning_noise) is not int or not 0 <= tuning_noise <= 3:
        raise ValueError("tuning_noise must be an integer from zero to three")
    if deadline_seconds is not None:
        try:
            valid_deadline = (type(deadline_seconds) in (int, float)
                              and math.isfinite(deadline_seconds) and deadline_seconds >= 0)
        except OverflowError:
            valid_deadline = False
        if not valid_deadline:
            raise ValueError("deadline_seconds must be finite and nonnegative")
    variants = _variants(config)
    keys = _noise_keys(config, tuning_noise)
    plan = _sample_plan(config, max_runs, seed, tuning_noise, steer, variants, keys)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "runs").mkdir()
    manifest = {"v": 1, "status": "running", "config_path": str(config.path),
                "config_hash": hashlib.sha256(config.path.read_bytes()).hexdigest(),
                "total_runs": 0, "planned_runs": [], "runs": [], "plan": plan,
                "max_runs": max_runs, "sampler_seed": seed, "steer": steer,
                "tuning_noise": tuning_noise, "deadline_seconds": deadline_seconds,
                "skipped_runs": [], "stop_reason": None}
    write_json(output_dir / "batch.json", manifest)
    write_json(output_dir / "exploration.json", manifest)
    control = ProcessControl()
    started = time.monotonic()
    deadline = None if deadline_seconds is None else started + deadline_seconds
    stop_reason = "run_budget"
    try:
        for spec in plan:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                stop_reason = "deadline"
                break
            active_config = config if remaining is None else replace(
                config, wall_timeout=min(config.wall_timeout, remaining))
            manifest["planned_runs"].append(spec)
            manifest["total_runs"] += 1
            write_json(output_dir / "batch.json", manifest)
            write_json(output_dir / "exploration.json", manifest)
            # Persisting a large plan can itself consume the final budget.
            # Do not turn that unlaunched draw into an attempted game.
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                manifest["planned_runs"].pop()
                manifest["total_runs"] -= 1
                stop_reason = "deadline"
                break
            active_config = config if remaining is None else replace(
                config, wall_timeout=min(config.wall_timeout, remaining))
            try:
                record = run_case(active_config, output_dir / "runs" / spec["id"],
                                  seed=spec["seed"], map_name=spec["map"], profile=spec["profile"],
                                  decider=spec["decider"], tuning=spec["tuning"],
                                  extra_args=tuple(spec["extra_args"]), control=control)
            except KeyboardInterrupt:
                control.cancel()
                record = _failed_record(config, output_dir, spec, "cancelled")
                record.update(variant=spec["variant"], extra_args=spec["extra_args"],
                              noise_keys=spec["noise_keys"])
                write_json(output_dir / "runs" / spec["id"] / "run.json", record)
                manifest["runs"].append(record)
                stop_reason = "interrupted"
                break
            except Exception as error:
                record = _failed_record(config, output_dir, spec, "execution_error", error)
            record.update(variant=spec["variant"], extra_args=spec["extra_args"],
                          noise_keys=spec["noise_keys"])
            write_json(output_dir / "runs" / spec["id"] / "run.json", record)
            manifest["runs"].append(record)
    except KeyboardInterrupt:
        control.cancel()
        stop_reason = "interrupted"
    attempted = {spec["id"] for spec in manifest["planned_runs"]}
    manifest["total_runs"] = len(manifest["planned_runs"])
    completed = {row["id"] for row in manifest["runs"]}
    for spec in manifest["planned_runs"]:
        if spec["id"] not in completed:
            manifest["runs"].append(_failed_record(config, output_dir, spec, "cancelled"))
    skipped_reason = "not_started_interrupted" if stop_reason == "interrupted" else "not_started_deadline"
    manifest["skipped_runs"] = [{"id": spec["id"], "reason": skipped_reason}
                                for spec in plan if spec["id"] not in attempted]
    write_json(output_dir / "batch.json", manifest)
    metrics = aggregate_batch(output_dir, bootstrap_samples=config.bootstrap_samples,
                              seed=config.bootstrap_seed)
    rejected = {row["id"]: row["reasons"] for row in metrics["invalid_runs"]}
    for record in manifest["runs"]:
        if record["id"] in rejected:
            record.update(valid=False, status="error", invalid_reasons=list(dict.fromkeys(
                [*record["invalid_reasons"], *rejected[record["id"]]])))
            write_json(output_dir / "runs" / record["id"] / "run.json", record)
    findings = [row for row in manifest["runs"] if not row["valid"] or row["status"] == "finding"]
    (output_dir / "findings.jsonl").write_bytes(b"".join(canonical_json(row) + b"\n" for row in findings))
    coverage = Counter((spec["map"], spec["profile"], spec["decider"], spec["variant"])
                       for spec in manifest["planned_runs"])
    manifest.update(status="interrupted" if stop_reason == "interrupted" else "completed",
                    stop_reason=stop_reason, findings=len(findings),
                    valid_runs=sum(row["valid"] for row in manifest["runs"]),
                    invalid_runs=sum(not row["valid"] for row in manifest["runs"]),
                    coverage=[dict(zip(("map", "profile", "decider", "variant"), cell),
                                   runs=coverage[cell])
                              for cell in itertools.product(config.maps, config.profiles,
                                                            config.backends,
                                                            [item["name"] for item in variants])])
    write_json(output_dir / "batch.json", manifest)
    write_json(output_dir / "exploration.json", manifest)
    return manifest
