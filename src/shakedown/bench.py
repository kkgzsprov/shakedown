"""Controlled tinyraid QA qualification, separate from open-world bug discovery."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import itertools
import json
from pathlib import Path
import sys

from .anomalies import DEFAULT_THRESHOLDS, analyze_batch, signal_observation, wilson_lower
from .config import load_config
from .explore import explore
from .orchestrate import child_environment, digest, prepare_work, run_case, write_json
from .processes import run_process
from .triage import triage_batch


DEFAULT_BACKENDS = ("rule", "random", "mock")
PROBE_NAMES = ("navigate_corner", "sprint_wall", "reload_fire", "heal_top_up",
               "unlock_door", "grenade_escape", "crouched_extract")
PRIMARY_CASES = {
    "corner_pocket": ("oracle", "stuck"),
    "thin_wall": ("oracle", "solid_collision"),
    "ammo_underflow": ("oracle", "numeric"),
    "door_no_key": ("oracle", "door_without_key"),
    "heal_overflow": ("oracle", "numeric"),
    "silent_grenade": ("anomaly", "unwarned_explosion_death"),
    "extract_crouch": ("anomaly", "extraction_zone_timeout"),
}
DIAGNOSTIC_CASES = ("vision_leak", "wallclock_load")
SAMPLER_SEED = 1729
MIN_RUNS = 5
# Deliberate waits in controlled workflows are not evidence of a timeout bug.
# Generic exploration keeps the ordinary defaults; this fixed test bed tests
# precisely the two observable behavioral faults described in chapter 8.
THRESHOLDS = {**{name: 1.0 for name in DEFAULT_THRESHOLDS},
              "unwarned_explosion_death": .25, "extraction_zone_timeout": .25}


def _load(path: Path):
    def reject(value):
        raise ValueError(f"nonfinite benchmark evidence: {value}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)


def _rows(folder: Path) -> list[dict]:
    return [json.loads(line) for line in (folder / "telemetry.jsonl").read_text(
        encoding="utf-8").splitlines()]


def _seed_manifest(root: Path) -> tuple[dict, str]:
    path = root / "examples/tinyraid/m3-seeds.json"
    manifest = _load(path)
    if type(manifest) is not dict or type(manifest.get("v")) is not int or manifest["v"] != 1:
        raise ValueError("invalid M3 seed manifest")
    for split in ("search", "holdout"):
        seeds = manifest.get(split)
        if (type(seeds) is not list or not seeds or any(type(seed) is not int for seed in seeds)
                or len(set(seeds)) != len(seeds)):
            raise ValueError("benchmark seed splits must contain unique integers")
    if set(manifest["search"]) & set(manifest["holdout"]):
        raise ValueError("search and holdout seeds must be disjoint")
    return manifest, hashlib.sha256(path.read_bytes()).hexdigest()


def _configuration(root: Path, seeds: list[int], backends: tuple[str, ...], bug: str | None):
    config = load_config(root / "examples/tinyraid/shakedown.toml")
    variants = [{"name": name, "args": ["--probe", name] + (["--bug", bug] if bug else [])}
                for name in PROBE_NAMES]
    raw = {**config.raw, "explore": {"variants": variants},
           "clips": {"replay_args": ["--frame-dir", "{frame_dir}", "--frame-every", "6"],
                     "fps": 10, "simulation_hz": 60}}
    return replace(config, seeds=tuple(seeds), maps=("yard",), profiles=("cautious",),
                   backends=backends, duration=14.0, tuning_candidates=({},), raw=raw)


def _plan_hash(plan: list[dict], bug: str | None = None) -> str:
    normalized = []
    for row in plan:
        entry = dict(row)
        arguments = list(entry.get("extra_args", []))
        if bug:
            if arguments[-2:] != ["--bug", bug]:
                raise ValueError("fault plan does not preserve the declared single-fault injection")
            arguments = arguments[:-2]
        entry["extra_args"] = arguments
        normalized.append(entry)
    return digest(normalized)


def _matches(entry: dict, mechanism: str, kind: str) -> bool:
    signature = entry.get("signature", {})
    return (entry.get("status") == "reproduced" and signature.get("mechanism") == mechanism
            and signature.get("oracle" if mechanism == "oracle" else "signal") == kind)


def _complete_coverage(batch: dict, seeds: list[int], backends: tuple[str, ...]) -> bool:
    expected = set(itertools.product(seeds, ("yard",), ("cautious",), backends, PROBE_NAMES))
    observed = [(row.get("seed"), row.get("map"), row.get("profile"), row.get("decider"),
                 row.get("variant")) for row in batch.get("runs", [])]
    return (batch.get("status") == "completed" and not batch.get("skipped_runs")
            and len(observed) == len(expected) and set(observed) == expected)


def _first_detectable(batch_dir: Path, batch: dict, anomalies: dict,
                      mechanism: str, kind: str) -> int | None:
    """Retrospective prefix estimate, not an online early-stop claim."""
    if mechanism == "oracle":
        for index, row in enumerate(batch["runs"], 1):
            if row.get("valid") and (row.get("result") or {}).get("oracle") == kind:
                return index
        return None
    groups = [row for row in anomalies["findings"] if row["signal"] == kind]
    counts = [[0, 0] for _ in groups]
    for index, metadata in enumerate(batch["runs"], 1):
        if metadata.get("valid") is not True:
            continue
        rows = _rows(batch_dir / "runs" / metadata["id"])
        value = signal_observation(kind, metadata, rows)
        if value is None:
            continue
        for group_index, group in enumerate(groups):
            if any(metadata.get(key, "default") != expected for key, expected in group["group"].items()):
                continue
            counts[group_index][0] += int(value)
            counts[group_index][1] += 1
            hits, n = counts[group_index]
            if (n >= MIN_RUNS and wilson_lower(hits, n) > group["threshold"]
                    and wilson_lower(hits, n) > group["overall_rate"]):
                return index
    return None


def _visibility_diagnostic(config, output: Path) -> dict:
    output.mkdir()
    results = []
    for index, fault in enumerate((False, True, True)):
        folder = output / ("clean" if index == 0 else f"fault-{index}")
        folder.mkdir()
        command = [sys.executable, "-m", "examples.tinyraid.checks"]
        if fault:
            command += ["--bug", "vision_leak"]
        code, timeout = run_process(command, cwd=prepare_work(config, folder),
                                    env=child_environment(config), stdout_path=folder / "stdout.log",
                                    timeout_s=config.wall_timeout)
        try:
            fixture = _load(folder / "stdout.log")
        except (OSError, ValueError, TypeError):
            fixture = None
        results.append({"fault_enabled": fault, "exit_code": code, "wall_timeout": timeout,
                        "fixture": fixture, "evidence": folder.name})
    clean = results[0]
    controls = (not clean["wall_timeout"] and clean["exit_code"] == 0
                and type(clean["fixture"]) is dict and clean["fixture"].get("passed") is True)
    detected = all(not row["wall_timeout"] and row["exit_code"] == 1
                   and type(row["fixture"]) is dict and row["fixture"].get("passed") is False
                   and row["fixture"].get("leaked_ids")
                   for row in results[1:])
    return {"case": "vision_leak", "mechanism": "C7", "status": "passed" if controls and detected else "failed",
            "reproduced": bool(controls and detected), "runs": results,
            "scope": "game-owned observation fixture with a visible positive control"}


def _clock_diagnostic(config, output: Path, seed: int) -> dict:
    output.mkdir()
    command = ["examples.tinyraid.clock_probe" if token == "examples.tinyraid" else token
               for token in config.command]
    controlled = replace(config, command=command, duration=2.0)
    results = []
    for name, bug in (("clean", None), ("fault-1", "wallclock_load"), ("fault-2", "wallclock_load")):
        folder = output / name
        folder.mkdir()
        extra = (["--bug", bug] if bug else [])
        first = run_case(controlled, folder / "slow", seed=seed, map_name="yard", profile="cautious",
                         decider="mock", extra_args=tuple([*extra, "--advance", ".2"]))
        replay = run_case(controlled, folder / "fast-tape", seed=seed, map_name="yard", profile="cautious",
                          decider="tape", tape_path=folder / "slow/decision_tape.jsonl",
                          extra_args=tuple([*extra, "--advance", "0"]))
        same = None
        if first.get("valid") and replay.get("valid"):
            original = [row for row in _rows(folder / "slow") if row["type"] != "run_begin"]
            repeated = [row for row in _rows(folder / "fast-tape") if row["type"] != "run_begin"]
            same = original == repeated
        results.append({"evidence": name, "equal_telemetry": same,
                        "original_valid": first.get("valid"), "tape_valid": replay.get("valid")})
    passed = results[0]["equal_telemetry"] is True and all(
        row["equal_telemetry"] is False for row in results[1:])
    return {"case": "wallclock_load", "mechanism": "C3", "status": "passed" if passed else "failed",
            "reproduced": passed, "controlled_host_clock": True, "runs": results,
            "scope": "controlled host-clock advance after the first real decision; not input-replay support"}


def _markdown(report: dict) -> str:
    lines = ["# Tinyraid benchmark", "", f"Qualification: **{report['status']}**", "",
             "Controlled input workflows test Shakedown mechanisms, not autonomous discovery in unknown games.",
             "Search and partial runs never qualify as held-out completion.", "",
             f"Split: {report['split']}; seeds: {report['seeds']}; budget per condition: {report['run_budget']}.",
             f"Clean findings: {report['clean']['findings']}; primary recall: {report['recall']['found']}/{report['recall']['total']}.",
             "", "| Case | Mechanism | Found | Reproduced | Findings matched / all | First detectable prefix |",
             "|---|---|---|---|---:|---:|"]
    for row in report["cases"]:
        lines.append(f"| {row['case']} | {row['mechanism']}:{row['kind']} | {row['found']} | "
                     f"{row['reproduced']} | {row['matched_findings']}/{row['findings']} | "
                     f"{row['retrospective_first_detectable_run']} |")
    lines += ["", "The prefix is reconstructed after the full fixed budget. It is not an observed online stopping time.",
              "All policy and independent input reproductions are retained in each condition's triage catalogue.",
              "", "## Separate conformance diagnostics", ""]
    lines += [f"- {row['case']}: {row['status']} ({row.get('scope', 'not selected')})"
              for row in report["diagnostics"]]
    return "\n".join(lines) + "\n"


def _bench_tinyraid(output_dir: Path, *, split: str, max_runs: int | None,
                    backends: list[str] | None, cases: list[str] | None,
                    clips: bool, checkpoint: dict) -> dict:
    """Run clean and single-fault conditions against exactly the same sampled plan."""
    if split not in {"search", "holdout"} or type(clips) is not bool:
        raise ValueError("benchmark split must be search or holdout; clips must be boolean")
    chosen_backends = tuple(DEFAULT_BACKENDS if backends is None else backends)
    selected = tuple([*PRIMARY_CASES, *DIAGNOSTIC_CASES] if cases is None else cases)
    if (not chosen_backends or any(name not in DEFAULT_BACKENDS for name in chosen_backends)
            or len(set(chosen_backends)) != len(chosen_backends)):
        raise ValueError("benchmark backends must be unique offline policies")
    if (not selected or any(name not in {*PRIMARY_CASES, *DIAGNOSTIC_CASES} for name in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("unknown or duplicate tinyraid benchmark case")
    if max_runs is not None and (type(max_runs) is not int or max_runs < 0):
        raise ValueError("benchmark run budget must be a nonnegative integer")
    root = Path(__file__).resolve().parents[2]
    seeds, seed_hash = _seed_manifest(root)
    full_budget = len(seeds[split]) * len(PROBE_NAMES) * len(chosen_backends)
    budget = full_budget if max_runs is None else max_runs
    base = _configuration(root, seeds[split], chosen_backends, None)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    report = {"v": 1, "status": "running", "split": split, "seeds": seeds[split],
              "seed_manifest_hash": seed_hash, "search_seeds": seeds["search"],
              "holdout_seeds": seeds["holdout"], "sampler_seed": SAMPLER_SEED,
              "backends": list(chosen_backends), "probes": list(PROBE_NAMES),
              "selected_cases": list(selected),
              "run_budget": budget, "full_run_budget": full_budget,
              "scope": "controlled input-workflow test bed; no claim about unknown-game bug discovery",
              "anomaly_settings": {"min_runs": MIN_RUNS, "thresholds": THRESHOLDS},
              "cases": [], "diagnostics": []}
    write_json(output_dir / "benchmark.json", report)
    checkpoint.update(report=report, output_dir=output_dir)
    clean_dir = output_dir / "clean"
    report.update(active_condition="clean", active_stage="exploration")
    clean_batch = explore(base, clean_dir, max_runs=budget, seed=SAMPLER_SEED)
    report["clean"] = {"runs": clean_batch["total_runs"], "invalid_runs": clean_batch["invalid_runs"],
                       "oracle_or_execution_findings": clean_batch["findings"],
                       "anomaly_findings": None, "findings": None, "evidence": "clean",
                       "status": clean_batch["status"],
                       "complete_coverage": _complete_coverage(clean_batch, seeds[split], chosen_backends)}
    if clean_batch["status"] == "interrupted":
        raise KeyboardInterrupt
    report["active_stage"] = "anomalies"
    clean_anomalies = analyze_batch(clean_dir, min_runs=MIN_RUNS, thresholds=THRESHOLDS)
    report["clean"]["anomaly_findings"] = len(clean_anomalies["findings"])
    report["active_stage"] = "triage"
    clean_triage = triage_batch(base, clean_dir, output_dir / "clean-triage", clips=clips)
    reference_hash = _plan_hash(clean_batch["plan"])
    report["plan_hash"] = reference_hash
    report["clean"]["findings"] = len(clean_triage["entries"])
    for bug in PRIMARY_CASES:
        if bug not in selected:
            continue
        config = _configuration(root, seeds[split], chosen_backends, bug)
        case_dir = output_dir / bug
        report.update(active_condition=bug, active_stage="exploration")
        batch = explore(config, case_dir, max_runs=budget, seed=SAMPLER_SEED)
        if batch["status"] == "interrupted":
            report["interrupted_condition"] = {"case": bug, "runs": batch["total_runs"],
                                               "invalid_runs": batch["invalid_runs"],
                                               "status": "interrupted", "evidence": bug}
            raise KeyboardInterrupt
        report["active_stage"] = "anomalies"
        anomalies = analyze_batch(case_dir, min_runs=MIN_RUNS, thresholds=THRESHOLDS,
                                  baseline_dir=clean_dir)
        report["active_stage"] = "triage"
        triaged = triage_batch(config, case_dir, output_dir / f"{bug}-triage", clips=clips)
        mechanism, kind = PRIMARY_CASES[bug]
        entries = triaged["entries"]
        matched = [entry for entry in entries if _matches(entry, mechanism, kind)]
        plan_equal = _plan_hash(batch["plan"], bug) == reference_hash
        found = any((entry.get("signature", {}).get("mechanism") == mechanism
                     and entry["signature"].get("oracle" if mechanism == "oracle" else "signal") == kind)
                    for entry in entries)
        row = {"case": bug, "mechanism": mechanism, "kind": kind,
               "found": found, "reproduced": bool(matched), "matched_findings": len(matched),
               "findings": len(entries), "precision": len(matched) / len(entries) if entries else None,
               "runs": batch["total_runs"], "invalid_runs": batch["invalid_runs"],
               "complete_coverage": _complete_coverage(batch, seeds[split], chosen_backends),
               "plan_equal": plan_equal, "normalized_plan_hash": _plan_hash(batch["plan"], bug),
               "retrospective_first_detectable_run": _first_detectable(case_dir, batch, anomalies, mechanism, kind),
               "actual_run_budget_consumed": batch["total_runs"], "cost_usd": 0.0,
               "signature_ids": [entry["id"] for entry in entries], "evidence": bug,
               "triage": f"{bug}-triage/triage.json"}
        report["cases"].append(row)
        write_json(output_dir / "benchmark.json", report)
    for name in DIAGNOSTIC_CASES:
        report.update(active_condition=name, active_stage="diagnostic")
        if name not in selected:
            result = {"case": name, "status": "unmeasured", "reproduced": False,
                      "scope": "not selected; excluded from primary recall"}
        elif name == "vision_leak":
            result = _visibility_diagnostic(base, output_dir / "diagnostic-visibility")
        else:
            result = _clock_diagnostic(base, output_dir / "diagnostic-clock", seeds[split][0])
        report["diagnostics"].append(result)
    completed = len(report["cases"])
    caught = sum(row["reproduced"] for row in report["cases"])
    all_findings = report["clean"]["findings"] + sum(row["findings"] for row in report["cases"])
    matched_findings = sum(row["matched_findings"] for row in report["cases"])
    report["recall"] = {"found": caught, "total": completed,
                        "rate": caught / completed if completed else None,
                        "full_primary_case_count": len(PRIMARY_CASES)}
    report["precision"] = {"matched": matched_findings, "findings": all_findings,
                           "rate": matched_findings / all_findings if all_findings else None,
                           "unit": "finding signatures"}
    qualified = (split == "holdout" and completed == len(PRIMARY_CASES)
                 and chosen_backends == DEFAULT_BACKENDS and budget == full_budget
                 and set(selected) == {*PRIMARY_CASES, *DIAGNOSTIC_CASES}
                 and report["clean"]["complete_coverage"]
                 and all(row["complete_coverage"] for row in report["cases"]))
    gate = (report["clean"]["findings"] == 0 and report["clean"]["invalid_runs"] == 0
            and caught == len(PRIMARY_CASES)
            and all(row["plan_equal"] and row["invalid_runs"] == 0 for row in report["cases"]))
    selected_diagnostics_ok = all(row["status"] != "failed" for row in report["diagnostics"])
    report.update(qualified=qualified, gate_passed=qualified and gate and selected_diagnostics_ok,
                  status=("passed" if gate and selected_diagnostics_ok else "failed") if qualified else "unqualified")
    report.pop("active_condition", None)
    report.pop("active_stage", None)
    write_json(output_dir / "benchmark.json", report)
    (output_dir / "benchmark.md").write_text(_markdown(report), encoding="utf-8")
    return report


def bench_tinyraid(output_dir: Path, *, split: str = "holdout", max_runs: int | None = None,
                   backends: list[str] | None = None, cases: list[str] | None = None,
                   clips: bool = False) -> dict:
    """Keep completed evidence on cancellation and never launch another condition."""
    checkpoint = {}
    try:
        return _bench_tinyraid(output_dir, split=split, max_runs=max_runs, backends=backends,
                              cases=cases, clips=clips, checkpoint=checkpoint)
    except KeyboardInterrupt:
        if not checkpoint:
            raise  # No output or attempted game exists yet.
        report, directory = checkpoint["report"], checkpoint["output_dir"]
        report.setdefault("clean", {"findings": None, "status": "interrupted"})
        completed = len(report["cases"])
        caught = sum(row["reproduced"] for row in report["cases"])
        report.update(status="interrupted", qualified=False, gate_passed=False,
                      recall={"found": caught, "total": completed,
                              "rate": caught / completed if completed else None,
                              "full_primary_case_count": len(PRIMARY_CASES)},
                      precision={"matched": sum(row["matched_findings"] for row in report["cases"]),
                                 "findings": None, "rate": None,
                                 "unit": "finding signatures; incomplete benchmark"})
        write_json(directory / "benchmark.json", report)
        (directory / "benchmark.md").write_text(_markdown(report), encoding="utf-8")
        return report
