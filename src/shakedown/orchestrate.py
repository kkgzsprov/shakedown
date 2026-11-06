"""Expand reproducible batches into bounded, isolated game executions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import shutil
from typing import TYPE_CHECKING

from .bridge import run_bridge
from .config import ProjectConfig, build_command
from .deciders import make_decider
from .processes import run_process
from .protocol import canonical_json

if TYPE_CHECKING:
    from .processes import ProcessControl


def write_json(path: Path, data) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical_json(data) + b"\n")
    temporary.replace(path)


def digest(data) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()


def child_environment(config: ProjectConfig) -> dict:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(config.root), str(Path(__file__).resolve().parents[1]),
        environment.get("PYTHONPATH", ""))))
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def prepare_work(config: ProjectConfig, run_dir: Path) -> Path:
    work = run_dir / "work"
    work.mkdir(parents=True, exist_ok=False)
    for name in config.fixtures:
        source = (config.root / name).resolve()
        source.relative_to(config.root.resolve())
        destination = work / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return work


def _reject_constant(value):
    raise ValueError(f"nonfinite JSON constant {value}")


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def _telemetry(path: Path) -> list[dict]:
    rows = []
    last_step, last_time = -1, -1.0
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line, parse_constant=_reject_constant)
        if (type(row) is not dict or type(row.get("type")) is not str
                or type(row.get("step")) is not int or row["step"] < last_step
                or type(row.get("t")) not in (int, float) or not math.isfinite(row["t"])
                or row["t"] < last_time):
            raise ValueError("invalid or nonmonotonic telemetry event")
        rows.append(row)
        last_step, last_time = row["step"], row["t"]
    if (not rows or rows[0]["type"] != "run_begin" or rows[-1]["type"] != "run_end"
            or sum(row["type"] == "run_begin" for row in rows) != 1
            or sum(row["type"] == "run_end" for row in rows) != 1
            or rows[-1].get("outcome") not in {"death", "extract", "timeout", "oracle"}):
        raise ValueError("missing or invalid telemetry boundaries")
    return rows


def _classify(record: dict, run_dir: Path, config: ProjectConfig) -> None:
    reasons = record["invalid_reasons"]
    code = record["exit_code"]
    codes = config.exit_codes
    if record["wall_timeout"]:
        reasons.append("wall_timeout")
    if code == codes["contract"]:
        reasons.append("contract_error")
    elif code == codes["divergence"]:
        reasons.append("divergence")
    elif code not in {codes["ok"], codes["oracle"]}:
        reasons.append("crash" if code is not None else "launch_error")
    try:
        rows = _telemetry(run_dir / "telemetry.jsonl")
        record["result"] = end = rows[-1]
        begin = rows[0]
        if ("seed" in begin and begin["seed"] != record["seed"]) or (
            "map" in begin and begin["map"] != record["map"]
        ):
            reasons.append("startup_mismatch")
        if (code == codes["oracle"]) != (end["outcome"] == "oracle"):
            reasons.append("outcome_exit_mismatch")
        if end["outcome"] == "oracle" and not any(
            event["type"] == "oracle" and event.get("oracle") == end.get("oracle")
            for event in rows
        ):
            reasons.append("missing_oracle_evidence")
        record["versions"]["adapter"] = begin.get("adapter_version")
    except (OSError, ValueError, TypeError, OverflowError) as error:
        reasons.append("invalid_telemetry")
        record["evidence_error"] = str(error)[:300]
    try:
        if type(_json(run_dir / "inputs.json")) not in (dict, list):
            raise ValueError("input recording must be an object or array")
    except (OSError, ValueError, TypeError):
        reasons.append("invalid_input_recording")
    summary_path = run_dir / "bridge_summary.json"
    if record["decider"] in {"random", "mock"}:
        try:
            summary = _json(summary_path)
            if type(summary) is not dict or summary.get("exit_code") != code:
                raise ValueError("invalid bridge summary")
            if summary.get("valid") is not True:
                reasons.extend(summary.get("invalid_reasons") or ["invalid_bridge_run"])
            if type(summary.get("game")) is dict:
                record["versions"]["adapter"] = summary["game"].get("adapter_version")
        except (OSError, ValueError, TypeError):
            reasons.append("invalid_bridge_summary")
    record["invalid_reasons"] = list(dict.fromkeys(reasons))
    record["valid"] = not reasons
    record["status"] = ("error" if reasons else "finding" if code == codes["oracle"] else "completed")


def _attempt_record(config: ProjectConfig, run_dir: Path, *, seed: int,
                    map_name: str, profile: str, decider: str,
                    seconds: float, tuning: dict, config_hash: str | None = None) -> dict:
    from . import __version__
    return {
        "v": 1, "id": run_dir.name, "seed": seed, "map": map_name, "profile": profile,
        "decider": decider, "tuning": tuning, "tuning_hash": digest(tuning),
        "seconds": seconds, "config_path": str(config.path),
        "config_hash": config_hash or hashlib.sha256(config.path.read_bytes()).hexdigest(),
        "planner_instructions_hash": hashlib.sha256(config.instructions.encode()).hexdigest(),
        "versions": {"shakedown": __version__, "adapter": None},
        "fixtures": list(config.fixtures), "exit_codes": dict(config.exit_codes),
        "command": [], "cwd": str(run_dir / "work"), "status": "running",
        "exit_code": None, "wall_timeout": False, "valid": False,
        "invalid_reasons": [], "result": None,
    }


def run_case(config: ProjectConfig, run_dir: Path, *, seed: int,
             map_name: str, profile: str, decider: str = "rule",
             duration: float | None = None, tuning: dict | None = None,
             tape_path: Path | None = None, replay_inputs: Path | None = None,
             extra_args: tuple[str, ...] = (), control: ProcessControl | None = None) -> dict:
    """Persist one attempt, including launch failures, without automatic retries."""
    run_dir = Path(run_dir).resolve()
    seconds = config.duration if duration is None else duration
    if (type(seed) is not int or type(seconds) not in (int, float)
            or not math.isfinite(seconds) or seconds <= 0 or type(tuning or {}) is not dict):
        raise ValueError("invalid run specification")
    if decider not in {"rule", "random", "mock", "tape"}:
        raise ValueError("unknown offline decider")
    if decider == "tape" and tape_path is None:
        raise ValueError("tape mode requires a tape path")
    if replay_inputs is not None and config.replay_command is None:
        raise ValueError("input replay command is not configured")
    run_dir.mkdir(parents=True, exist_ok=False)
    values = dict(tuning or {})
    record = _attempt_record(config, run_dir, seed=seed, map_name=map_name, profile=profile,
                             decider=decider, seconds=seconds, tuning=values)
    record["extra_args"] = list(extra_args)
    if replay_inputs is not None:
        record["replay_inputs"] = str(Path(replay_inputs).resolve())
    if control is not None and control.cancelled.is_set():
        record.update(status="error", invalid_reasons=["cancelled"])
        write_json(run_dir / "run.json", record)
        return record
    try:
        work = prepare_work(config, run_dir)
        tuning_file = run_dir / "tuning.json"
        write_json(tuning_file, {"tuning_version": 1, "values": values})
        mode = "bridge" if decider in {"random", "mock"} else "rule"
        if decider == "tape":
            mode = f"tape:{Path(tape_path).resolve()}"
        command = build_command(config, run_dir=run_dir, seed=seed, map_name=map_name,
                                profile=profile, decider_mode=mode, duration=seconds,
                                tuning_file=tuning_file,
                                inputs_file=Path(replay_inputs).resolve() if replay_inputs else None,
                                replay=replay_inputs is not None, extra_args=extra_args)
        record["command"] = command
        record["mode"] = "inputs" if replay_inputs else mode
        write_json(run_dir / "run.json", record)
        environment = child_environment(config)
        if decider in {"random", "mock"} and replay_inputs is None:
            record["exit_code"] = run_bridge(
                command, run_dir, make_decider(decider, seed=seed),
                instructions=config.instructions, decider_name=decider,
                wall_timeout_s=config.wall_timeout, cwd=work, env=environment, control=control)
            summary = _json(run_dir / "bridge_summary.json")
            record["wall_timeout"] = summary.get("wall_timeout", False)
        else:
            record["exit_code"], record["wall_timeout"] = run_process(
                command, cwd=work, env=environment, stdout_path=run_dir / "stdout.log",
                timeout_s=config.wall_timeout, control=control)
        # Replay adapters do not need to write a second recording; preserve the
        # exact original inputs alongside this attempt for evidence inspection.
        if replay_inputs is not None and not (run_dir / "inputs.json").exists():
            shutil.copyfile(replay_inputs, run_dir / "inputs.json")
    except Exception as error:
        record["invalid_reasons"].append("execution_error")
        record["error"] = f"{type(error).__name__}: {error}"[:500]
    if control is not None and control.cancelled.is_set():
        record["invalid_reasons"].append("cancelled")
    try:
        _classify(record, run_dir, config)
    except Exception as error:
        record.update(valid=False, status="error")
        record["invalid_reasons"].append("classification_error")
        record["error"] = f"{type(error).__name__}: {error}"[:500]
    write_json(run_dir / "run.json", record)
    return record


def run_batch(config: ProjectConfig, output_dir: Path, *, workers: int | None = None,
              seeds: list[int] | None = None, backends: list[str] | None = None,
              duration: float | None = None) -> dict:
    from .aggregate import aggregate_batch
    from .processes import ProcessControl

    count_workers = config.workers if workers is None else workers
    selected_seeds = tuple(config.seeds if seeds is None else seeds)
    selected_backends = tuple(config.backends if backends is None else backends)
    seconds = config.duration if duration is None else duration
    if type(count_workers) is not int or not 1 <= count_workers <= 64:
        raise ValueError("workers must be an integer between 1 and 64")
    if (not selected_seeds or any(type(seed) is not int for seed in selected_seeds)
            or len(set(selected_seeds)) != len(selected_seeds)):
        raise ValueError("seeds must be nonempty unique integers")
    if (not selected_backends or any(name not in {"rule", "random", "mock"} for name in selected_backends)
            or len(set(selected_backends)) != len(selected_backends)):
        raise ValueError("batch deciders must be unique offline backends: rule, random, mock")
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("seconds must be finite and positive")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "runs").mkdir()
    combinations = list(itertools.product(selected_seeds, config.maps, config.profiles,
                                           selected_backends, config.tuning_candidates))
    planned = [{"id": f"r{index:06d}", "seed": seed, "map": map_name,
                "profile": profile, "decider": decider, "tuning": dict(tuning),
                "tuning_hash": digest(tuning), "seconds": seconds}
               for index, (seed, map_name, profile, decider, tuning) in enumerate(combinations, 1)]
    manifest = {"v": 1, "status": "running", "config_path": str(config.path),
                "config_hash": hashlib.sha256(config.path.read_bytes()).hexdigest(),
                "workers": count_workers, "total_runs": len(combinations),
                "planned_runs": planned, "runs": []}
    write_json(output_dir / "batch.json", manifest)
    results = {}
    jobs = {}
    control = ProcessControl()
    executor = ThreadPoolExecutor(max_workers=count_workers)
    interrupted = False

    def failed_attempt(spec: dict, reason: str, error: BaseException | None = None) -> dict:
        folder = output_dir / "runs" / spec["id"]
        folder.mkdir(parents=True, exist_ok=True)
        try:
            record = _json(folder / "run.json")
            if type(record) is not dict or type(record.get("invalid_reasons")) is not list:
                raise ValueError("invalid existing attempt")
        except (OSError, ValueError, TypeError):
            record = _attempt_record(
                config, folder, seed=spec["seed"], map_name=spec["map"], profile=spec["profile"],
                decider=spec["decider"], seconds=spec["seconds"], tuning=spec["tuning"],
                config_hash=manifest["config_hash"])
        record.update(status="error", valid=False,
                      invalid_reasons=list(dict.fromkeys([*record["invalid_reasons"], reason])))
        if error is not None:
            record["error"] = f"{type(error).__name__}: {error}"[:500]
        write_json(folder / "run.json", record)
        return record

    def collect(future, spec: dict) -> None:
        if future.cancelled():
            results[spec["id"]] = failed_attempt(spec, "cancelled")
            return
        try:
            results[spec["id"]] = future.result()
        except Exception as error:
            results[spec["id"]] = failed_attempt(spec, "worker_error", error)

    try:
        for spec in planned:
            run_id = spec["id"]
            future = executor.submit(run_case, config, output_dir / "runs" / run_id,
                                     seed=spec["seed"], map_name=spec["map"], profile=spec["profile"],
                                     decider=spec["decider"], duration=seconds, tuning=spec["tuning"],
                                     control=control)
            jobs[future] = spec
        for future in as_completed(jobs):
            collect(future, jobs[future])
    except KeyboardInterrupt:
        interrupted = True
        control.cancel()
        for future in jobs:
            future.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=interrupted)
    # Every planned row is retained even if submission or collection was
    # interrupted. Completed attempts keep their original verdicts.
    for future, spec in jobs.items():
        if spec["id"] not in results:
            collect(future, spec)
    for spec in planned:
        if spec["id"] not in results:
            results[spec["id"]] = failed_attempt(spec, "cancelled")
    rows = [results[key] for key in sorted(results)]
    metrics = aggregate_batch(output_dir, bootstrap_samples=config.bootstrap_samples,
                              seed=config.bootstrap_seed)
    invalid_ids = {row["id"] for row in metrics["invalid_runs"]}
    # Aggregation independently validates evidence. Propagate those verdicts to
    # execution receipts, so the batch cannot call rejected telemetry valid.
    for row in rows:
        if row["id"] in invalid_ids:
            entry = next(item for item in metrics["invalid_runs"] if item["id"] == row["id"])
            row["valid"], row["status"] = False, "error"
            row["invalid_reasons"] = list(dict.fromkeys(row["invalid_reasons"] + entry["reasons"]))
            write_json(output_dir / "runs" / row["id"] / "run.json", row)
    findings = [row for row in rows if not row["valid"] or row["status"] == "finding"]
    (output_dir / "findings.jsonl").write_bytes(b"".join(canonical_json(row) + b"\n" for row in findings))
    manifest.update(status="interrupted" if interrupted else "completed", runs=rows,
                    valid_runs=sum(row["valid"] for row in rows),
                    invalid_runs=sum(not row["valid"] for row in rows), findings=len(findings))
    write_json(output_dir / "batch.json", manifest)
    return manifest
