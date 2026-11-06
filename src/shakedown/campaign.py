"""Approved numeric search followed by a single, frozen holdout evaluation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path

from .anomalies import analyze_batch
from .balance_gate import evaluate_gate
from .optimize import candidates, defaults, target_score
from .orchestrate import digest, run_batch, write_json
from .planner.engine import approved_campaign, context, read_json, run_step
from .proposals import write_proposal


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _cells(document):
    cells = {}
    for experiment in document["experiments"]:
        # A candidate represents one game configuration across all conditions.
        if experiment["tuning_ranges"] != document["search_ranges"]:
            raise ValueError("balance experiments must share the campaign search_ranges")
        for profile in experiment["profiles"]:
            for map_name in experiment["maps"]:
                cell = cells.setdefault((profile, map_name), set())
                cell.update(experiment["seeds"][:document["batch_size"]])
    return {key: tuple(sorted(seeds)) for key, seeds in sorted(cells.items())}


def _metrics(groups, *, invalid=None):
    return {"v": 1, "groups": deepcopy(groups), "invalid_runs": invalid or [],
            "valid_runs": sum(group["runs"] for group in groups),
            "total_runs": sum(group["runs"] for group in groups) + len(invalid or [])}


def _revision_ranges(revision, approved, current):
    """A planner may change numeric ranges, never expand its human-approved box."""
    result = deepcopy(current)
    for key, requested in revision["search_ranges"].items():
        if key not in approved:
            raise ValueError(f"revision introduces an unapproved tuning key: {key}")
        bounds = approved[key]
        low = min(bounds["max"], max(bounds["min"], requested["min"]))
        high = min(bounds["max"], max(bounds["min"], requested["max"]))
        if (low, high) != (requested["min"], requested["max"]):
            revision["clipping"].append({"key": key, "scope": "approved_campaign",
                "requested_min": requested["min"], "requested_max": requested["max"],
                "applied_min": low, "applied_max": high})
        result[key] = {"min": low, "max": high}
    revision["search_ranges"] = deepcopy(result)
    return result


def _report(receipt, gates):
    lines = ["# Balance campaign evidence", "", "## Anomalies", ""]
    lines += ([f"- {row['batch']}: {row['findings']} signal(s); investigate separately."
               for row in receipt["anomalies"] if row["findings"]]
              or ["No anomaly signals in the executed batches. This is not a fairness verdict."])
    lines += ["", f"Status: {receipt['status']}",
              f"Policy fingerprint: `{receipt['policy_hash']}`",
              f"Attempted/planned-admitted runs: {receipt['attempted_runs']}", "",
              "## Holdout gates", "", "| Candidate | Status | Baseline score | Candidate score | Reasons |",
              "|---|---|---|---|---|"]
    for gate in gates:
        lines.append(f"| {gate['candidate_hash']} | {gate['status']} | {gate.get('baseline_score')} | "
                     f"{gate.get('candidate_score')} | {', '.join(gate['reasons'])} |")
    lines += ["", "## Proposed defaults — pending human review", "",
              "| Candidate | Patch |", "|---|---|"]
    lines += [f"| {row['candidate_hash']} | {row['path']}/defaults.patch |" for row in receipt["proposals"]]
    lines += ["", "## Limits", "",
              "- Gates compare per-run metric means. Bootstrap intervals remain descriptive evidence.",
              "- Policy sensitivity is checked only for the listed offline policies; human play and hosted models are unmeasured.",
              "- No defaults were applied and no game changes were committed.",
              "- Holdout seeds were excluded from search and revisions; selected candidates were frozen before evaluation."]
    return "\n".join(lines) + "\n"


def run_balance_campaign(config, campaign, approval, output_dir, *, approved=False,
                         targets, targets_approval, max_runs=None) -> dict:
    """Search within approved ranges; emit proposals only after held-out gates."""
    if approved is not True:
        raise ValueError("explicit balance campaign execution approval is required")
    document = approved_campaign(config, campaign, approval, targets, targets_approval)
    target_document = read_json(targets)
    settings = config.raw.get("balance", {})
    cells = _cells(document)
    scopes = list(cells)
    policies = list(settings.get("policies", config.backends))
    if any(name not in {"rule", "mock", "random"} for name in policies):
        raise ValueError("balance policy qualification currently supports offline deciders only")
    holdout = settings.get("holdout_seeds", [])
    min_runs = settings.get("min_runs", 5)
    if not holdout or len(holdout) < min_runs:
        raise ValueError("balance requires at least min_runs reserved holdout seeds")
    # Even unused configured search seeds are unavailable to the holdout set.
    if set(holdout) & set(config.seeds):
        raise ValueError("search and holdout seeds must be disjoint")
    if any(len(seeds) < min_runs for seeds in cells.values()):
        raise ValueError("every search scope needs at least min_runs unique seeds")
    table = context(config)["tuning_keys"]
    baseline = defaults(table)
    if not document["search_ranges"]:
        raise ValueError("balance search needs at least one approved tuning range")
    # Validate ranges and search settings before any filesystem mutations/children.
    count = settings.get("candidates_per_round", 4)
    options = {"method": settings.get("method", "random"),
               "grid_points": settings.get("grid_points", 3), "baseline": baseline}
    candidates(table, document["search_ranges"], count=1, seed=settings.get("seed", 0), **options)
    # Validate target identities/scopes now; missing measurements are expected here.
    target_score({"groups": []}, target_document, tuning_hash=digest(baseline),
                 scopes=scopes, policies=policies, min_runs=min_runs)
    source = (config.path.parent / config.raw["tuning"]["keys"]).resolve()
    if not source.is_relative_to(config.root):
        raise ValueError("balance default table must be inside the project root")
    policy_files = settings.get("policy_files", [])
    if not policy_files:
        raise ValueError("balance.policy_files must identify the game's executor and policy sources")
    runtime_files = []
    for name in policy_files:
        path = (config.root / name).resolve()
        if not path.is_relative_to(config.root) or not path.is_file():
            raise ValueError("balance.policy_files must be files inside the project root")
        runtime_files.append(path)
    package = Path(__file__).parent
    runtime_files.extend([package / "bridge.py", package / "protocol.py"])
    runtime_files.extend(sorted((package / "deciders").glob("*.py")))
    configured_limit = settings.get("max_runs", 1000)
    if max_runs is not None and (type(max_runs) is not int or max_runs < 1):
        raise ValueError("max_runs must be a positive integer")
    limit = configured_limit if max_runs is None else min(configured_limit, max_runs)
    top_k = settings.get("top_k", 1)
    search_size = sum(len(seeds) for seeds in cells.values()) * len(policies)
    holdout_size = len(holdout) * len(cells) * len(policies)
    bound = search_size * (1 + document["rounds"] * count) + holdout_size * (1 + top_k)
    if bound > limit:
        raise ValueError(f"campaign may require {bound} runs, exceeding max_runs={limit}")
    frozen_files = {str(Path(path).resolve()): _hash(path)
                    for path in (config.path, source, campaign, targets, approval, targets_approval)}
    runtime_hashes = {str(path): _hash(path) for path in runtime_files}
    frozen_files.update(runtime_hashes)
    instruction_file = config.raw.get("decider", {}).get("instructions")
    if instruction_file:
        path = (config.path.parent / instruction_file).resolve()
        frozen_files[str(path)] = _hash(path)
    for fixture in config.fixtures:
        path = config.root / fixture
        frozen_files[str(path)] = _hash(path)
    policy = {"deciders": policies, "instructions": document["profile_instructions"],
              "executor_profiles": sorted({profile for profile, _ in scopes}),
              "game_command": config.command, "duration": config.duration,
              "runtime_sources": runtime_hashes,
              "model_ids": {name: f"offline:{name}" for name in policies}}
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt = {"v": 1, "status": "running", "policy": policy, "policy_hash": digest(policy),
               "campaign_hash": digest(document), "targets_hash": digest(target_document),
               "source_hashes": frozen_files, "baseline": baseline, "search_seeds": sorted(
                   {seed for seeds in cells.values() for seed in seeds}),
               "holdout_seeds": list(holdout), "holdout_used": False, "max_runs": limit,
               "run_upper_bound": bound, "attempted_runs": 0, "batches": [], "rounds": [],
               "selected": [], "gates": [], "proposals": [], "anomalies": []}
    receipt_path = output_dir / "balance_run.json"
    write_json(receipt_path, receipt)
    search_groups = []
    all_candidates = {digest(baseline): baseline}

    def unchanged():
        if any(_hash(Path(path)) != expected for path, expected in frozen_files.items()):
            raise ValueError("approved inputs or policy fixtures changed during the campaign")

    def run_stage(name, tunings, *, held_out=False):
        stage_groups = []
        for index, ((profile, map_name), search_seeds) in enumerate(cells.items(), 1):
            unchanged()
            seeds = tuple(holdout) if held_out else search_seeds
            planned = len(seeds) * len(tunings) * len(policies)
            if receipt["attempted_runs"] + planned > limit:
                raise ValueError("campaign run budget is exhausted")
            relative = f"{name}/c{index:04d}"
            batch = {"path": relative, "profile": profile, "map": map_name,
                     "planned_runs": planned, "status": "running", "holdout": held_out}
            receipt["batches"].append(batch)
            receipt["attempted_runs"] += planned
            write_json(receipt_path, receipt)
            derived = replace(config, profiles=(profile,), maps=(map_name,), seeds=seeds,
                              backends=tuple(policies), tuning_candidates=tuple(tunings),
                              instructions=document["profile_instructions"][profile])
            path = output_dir / relative
            manifest = run_batch(derived, path)
            batch.update(status=manifest["status"], valid_runs=manifest["valid_runs"],
                         invalid_runs=manifest["invalid_runs"])
            if manifest["status"] == "interrupted":
                raise KeyboardInterrupt
            if manifest["status"] != "completed" or manifest["invalid_runs"]:
                raise ValueError("invalid or incomplete balance batch; holdout proposal blocked")
            measured = read_json(path / "metrics.json")
            if measured.get("invalid_runs"):
                raise ValueError("aggregation rejected balance evidence")
            stage_groups.extend({**group, "source_batch": relative} for group in measured["groups"])
            anomaly = config.raw.get("anomalies", {})
            signals = analyze_batch(path, min_runs=anomaly.get("min_runs", 5),
                thresholds=anomaly.get("thresholds"),
                group_by=anomaly.get("group_by", ("map", "profile", "variant")))
            receipt["anomalies"].append({"batch": relative, "findings": len(signals["findings"])})
            write_json(receipt_path, receipt)
        return stage_groups

    try:
        search_groups.extend(run_stage("search/baseline", [baseline]))
        ranges = deepcopy(document["search_ranges"])
        for round_index in range(document["rounds"]):
            proposed = candidates(table, ranges, count=count,
                seed=settings.get("seed", 0) + round_index, exclude=list(all_candidates), **options)
            if not proposed:
                receipt["stop_reason"] = "search_space_exhausted"
                break
            search_groups.extend(run_stage(f"search/round-{round_index+1:03d}", proposed))
            for tuning in proposed:
                all_candidates[digest(tuning)] = tuning
            search_metrics = _metrics(search_groups)
            metric_path = output_dir / "search/metrics.json"
            write_json(metric_path, search_metrics)
            revision_dir = output_dir / f"revisions/r{round_index+1:03d}"
            if settings.get("planner_revisions", False):
                planned = run_step(config, "revise", revision_dir, inputs={"metrics": metric_path})
                revision_path = Path(planned["artifact"])
                revision = read_json(revision_path)
                ranges = _revision_ranges(revision, document["search_ranges"], ranges)
            else:
                revision_dir.mkdir(parents=True)
                revision_path = revision_dir / "revision.json"
                revision = {"schema_version": 1, "hypotheses": [], "search_ranges": ranges,
                    "decision": "stop" if round_index + 1 == document["rounds"] else "continue",
                    "reason": "Fixed approved numeric-search schedule; no planner call.", "clipping": []}
            write_json(revision_path, revision)
            receipt["rounds"].append({"round": round_index+1,
                "candidates": [digest(tuning) for tuning in proposed],
                "revision": revision_path.relative_to(output_dir).as_posix(), "decision": revision["decision"]})
            write_json(receipt_path, receipt)
            if revision["decision"] == "stop":
                receipt["stop_reason"] = "revision_stop"
                break
        search_metrics = _metrics(search_groups)
        write_json(output_dir / "search/metrics.json", search_metrics)
        ranking = []
        baseline_hash = digest(baseline)
        baseline_score = target_score(search_metrics, target_document, tuning_hash=baseline_hash,
                                      scopes=scopes, policies=policies, min_runs=min_runs)
        for key, tuning in all_candidates.items():
            if key == baseline_hash:
                continue
            score = target_score(search_metrics, target_document, tuning_hash=key,
                                 scopes=scopes, policies=policies, min_runs=min_runs)
            if score["status"] == "measured":
                ranking.append({"hash": key, "tuning": tuning, "score": score["score"]})
        ranking.sort(key=lambda row: (row["score"], row["hash"]))
        if baseline_score["status"] == "measured":
            receipt["selected"] = [row for row in ranking if row["score"] < baseline_score["score"]][:top_k]
        # Freeze identities and selection using search evidence only, before any holdout run.
        write_json(output_dir / "selection.json", {"v": 1, "selection_source": "search_only",
            "baseline": baseline_score, "ranking": ranking, "selected": receipt["selected"],
            "holdout_seeds": list(holdout), "policy_hash": receipt["policy_hash"]})
        write_json(receipt_path, receipt)
        if receipt["selected"]:
            receipt["holdout_used"] = True
            write_json(receipt_path, receipt)
            held_out = _metrics(run_stage("holdout", [baseline] +
                                          [row["tuning"] for row in receipt["selected"]], held_out=True))
            write_json(output_dir / "holdout/metrics.json", held_out)
            for index, selected in enumerate(receipt["selected"], 1):
                gate = evaluate_gate(held_out, target_document, baseline=baseline,
                    candidate=selected["tuning"], scopes=scopes, policies=policies,
                    min_runs=min_runs, min_gain=settings.get("min_gain", .2))
                gate["candidate_hash"] = selected["hash"]
                receipt["gates"].append(gate)
                if gate["status"] == "passed":
                    unchanged()
                    relative = f"proposals/p{index:03d}"
                    write_proposal(config, selected["tuning"], output_dir / relative,
                                   gate=gate, source_sha256=frozen_files[str(source)])
                    receipt["proposals"].append({"candidate_hash": selected["hash"], "path": relative})
        else:
            receipt["stop_reason"] = "no_measured_search_improvement"
        unchanged()
        receipt["status"] = "completed"
        # Draft report is supplemental; the deterministic gate above controls proposals.
        report_inputs = {"metrics": output_dir / "search/metrics.json"}
        if receipt["holdout_used"]:
            report_inputs["holdout"] = output_dir / "holdout/metrics.json"
        run_step(config, "report", output_dir / "planner-report", inputs=report_inputs)
        unchanged()
        return receipt
    except BaseException as error:
        receipt["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        receipt["error"] = f"{type(error).__name__}: {error}"[:500]
        # A failed campaign never leaves an apparently usable proposal receipt.
        for row in receipt["proposals"]:
            proposal_path = output_dir / row["path"] / "proposal.json"
            invalidated = read_json(proposal_path)
            invalidated.update(status="invalidated", invalidation=receipt["error"])
            write_json(proposal_path, invalidated)
        raise
    finally:
        write_json(receipt_path, receipt)
        (output_dir / "report.md").write_text(_report(receipt, receipt["gates"]), encoding="utf-8")
