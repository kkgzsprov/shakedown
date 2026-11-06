"""Command-line entry points for Shakedown."""

import argparse
import json
import math
import os
from pathlib import Path
import sys

from .bridge import run_bridge
from .deciders import make_decider


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("wall timeout must be a number") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("wall timeout must be finite and positive")
    return seconds


def _seeds(value: str) -> list[int]:
    try:
        values = [int(item) for item in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError("seeds must be comma-separated integers") from error
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("seeds must be unique")
    return values


def _backends(value: str) -> list[str]:
    values = value.split(",")
    if (any(item not in {"rule", "random", "mock", "jev"} for item in values)
            or len(set(values)) != len(values)):
        raise argparse.ArgumentTypeError("deciders must be unique names: rule, random, mock, jev")
    return values


def _anomaly_options(config) -> dict:
    settings = config.raw.get("anomalies", {})
    if set(settings) - {"min_runs", "thresholds", "group_by"}:
        raise ValueError("unknown anomaly configuration key")
    return {"min_runs": settings.get("min_runs", 5),
            "thresholds": settings.get("thresholds"),
            "group_by": settings.get("group_by", ("map", "profile", "variant"))}


def _analyze(config, directory: Path, baseline: Path | None = None) -> dict:
    from .anomalies import analyze_batch
    return analyze_batch(directory, baseline_dir=baseline, **_anomaly_options(config))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shakedown")
    subparsers = parser.add_subparsers(dest="command", required=True)
    bridge = subparsers.add_parser("bridge", help="connect a game to a local decider")
    bridge.add_argument("--decider", choices=("random", "mock", "tape", "jev"), required=True)
    bridge.add_argument("--config", type=Path, help="hosted decider configuration; required for jev")
    bridge.add_argument("--tape")
    bridge.add_argument("--seed", type=int, default=0)
    bridge.add_argument("--instructions", type=Path)
    bridge.add_argument("--wall-timeout", type=_positive_seconds)
    bridge.add_argument("--run-dir", type=Path, required=True)
    bridge.add_argument("game_command", nargs=argparse.REMAINDER)
    batch = subparsers.add_parser("run", help="run an isolated batch and aggregate its evidence")
    batch.add_argument("--config", type=Path, default=Path("shakedown.toml"))
    batch.add_argument("--output", type=Path, required=True)
    batch.add_argument("--workers", type=int)
    batch.add_argument("--seeds", type=_seeds)
    batch.add_argument("--deciders", type=_backends)
    batch.add_argument("--seconds", type=_positive_seconds)
    checks = subparsers.add_parser("check-adapter", help="measure adapter checks C1 through C7")
    checks.add_argument("--config", type=Path, default=Path("shakedown.toml"))
    checks.add_argument("--output", type=Path, required=True)
    checks.add_argument("--seed", type=int)
    checks.add_argument("--map", dest="map_name")
    checks.add_argument("--profile")
    exploration = subparsers.add_parser("explore", help="sample reproducible QA runs within a budget")
    exploration.add_argument("--config", type=Path, default=Path("shakedown.toml"))
    exploration.add_argument("--output", type=Path, required=True)
    exploration.add_argument("--runs", type=int, default=60)
    exploration.add_argument("--deadline", type=float, help="maximum wall-clock seconds, including active run")
    exploration.add_argument("--seed", type=int, default=0, help="sampler seed")
    exploration.add_argument("--tuning-noise", type=int, default=0)
    exploration.add_argument("--no-steer", action="store_true")
    exploration.add_argument("--baseline", type=Path)
    anomaly = subparsers.add_parser("anomalies", help="recompute measured QA signals from batch evidence")
    anomaly.add_argument("--config", type=Path, default=Path("shakedown.toml"))
    anomaly.add_argument("--batch", type=Path, required=True)
    anomaly.add_argument("--baseline", type=Path)
    triage = subparsers.add_parser("triage", help="group findings and verify independent reproductions")
    triage.add_argument("--config", type=Path, default=Path("shakedown.toml"))
    triage.add_argument("--batch", type=Path, required=True)
    triage.add_argument("--output", type=Path, required=True)
    triage.add_argument("--clips", action="store_true")
    benchmark = subparsers.add_parser("bench", help="measure the tinyraid fault-injection test bed")
    benchmark.add_argument("game", choices=("tinyraid", "tinyraid-balance"))
    benchmark.add_argument("--output", type=Path, required=True)
    benchmark.add_argument("--split", choices=("search", "holdout"), default="holdout")
    benchmark.add_argument("--runs", type=int)
    benchmark.add_argument("--deciders", type=_backends)
    benchmark.add_argument("--cases", help="comma-separated fault cases; a subset is not a full qualification")
    benchmark.add_argument("--clips", action="store_true")
    plan = subparsers.add_parser("plan", help="draft a between-run planner document")
    plan.add_argument("step", choices=("targets", "campaign", "revise", "bug", "report"))
    plan.add_argument("--config", type=Path, default=Path("shakedown.toml"))
    plan.add_argument("--input", action="append", default=[], metavar="NAME=PATH")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--backend", choices=("mock", "claude-cli", "codex-cli"))
    plan.add_argument("--targets", type=Path)
    plan.add_argument("--targets-approval", type=Path)
    plan.add_argument("--budget-usd", type=float)
    plan.add_argument("--call-budget-usd", type=float)
    plan.add_argument("--max-calls", type=int)
    plan.add_argument("--ledger", type=Path)
    approval = subparsers.add_parser("approve", help="record an explicit operator approval")
    approval.add_argument("kind", choices=("targets", "campaign"))
    approval.add_argument("--config", type=Path, default=Path("shakedown.toml"))
    approval.add_argument("--artifact", type=Path, required=True)
    approval.add_argument("--receipt", type=Path, required=True)
    approval.add_argument("--approved", action="store_true")
    approval.add_argument("--targets", type=Path)
    approval.add_argument("--targets-approval", type=Path)
    campaign = subparsers.add_parser("campaign", help="execute an approved baseline or balance search")
    campaign.add_argument("--config", type=Path, default=Path("shakedown.toml"))
    campaign.add_argument("--campaign", type=Path, required=True)
    campaign.add_argument("--approval", type=Path, required=True)
    campaign.add_argument("--targets", type=Path, required=True)
    campaign.add_argument("--targets-approval", type=Path, required=True)
    campaign.add_argument("--output", type=Path, required=True)
    campaign.add_argument("--approved", action="store_true")
    campaign.add_argument("--max-runs", type=int, default=1000)
    campaign.add_argument("--optimize", action="store_true", help="search and evaluate frozen holdout candidates using [balance]")
    args = parser.parse_args(argv)
    if args.command in {"plan", "approve", "campaign"}:
        from .config import load_config
        from .planner.engine import approve_plan, run_step
        try:
            config = load_config(args.config)
            if args.command == "plan":
                inputs = {}
                for entry in args.input:
                    name, separator, value = entry.partition("=")
                    if not separator or not name or not value or name in inputs:
                        raise ValueError("inputs must be unique NAME=PATH arguments")
                    inputs[name] = Path(value)
                result = run_step(config, args.step, args.output, inputs=inputs,
                    backend=args.backend, targets=args.targets, targets_approval=args.targets_approval,
                    budget_usd=args.budget_usd, call_budget_usd=args.call_budget_usd,
                    max_calls=args.max_calls, ledger_path=args.ledger)
            elif args.command == "approve":
                result = approve_plan(config, args.kind, args.artifact, args.receipt,
                    approved=args.approved, targets=args.targets, targets_approval=args.targets_approval)
            else:
                if args.optimize:
                    from .campaign import run_balance_campaign as execute_campaign
                else:
                    from .planner.campaign import run_approved_campaign as execute_campaign
                result = execute_campaign(config, args.campaign, args.approval, args.output,
                    approved=args.approved, targets=args.targets, targets_approval=args.targets_approval,
                    max_runs=args.max_runs)
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            if result.get("status") == "interrupted":
                return 130
            return 0 if result.get("status", "passed") in {"passed", "completed"} else 1
        except (OSError, ValueError) as error:
            parser.error(str(error))
    if args.command == "bench":
        from .bench import bench_tinyraid
        try:
            if args.game == "tinyraid-balance":
                if args.runs is not None or args.deciders is not None or args.cases is not None or args.clips or args.split != "holdout":
                    raise ValueError("tinyraid-balance uses its fixed seed, policy and run qualification plan")
                from .balance_bench import bench_balance
                result = bench_balance(args.output)
            else:
                result = bench_tinyraid(args.output, split=args.split, max_runs=args.runs,
                                    backends=args.deciders,
                                    cases=args.cases.split(",") if args.cases is not None else None,
                                    clips=args.clips)
            print(json.dumps({key: value for key, value in result.items()
                              if key not in {"cases", "diagnostics", "plan"}}, ensure_ascii=False))
            if result.get("status") == "interrupted":
                return 130
            return 0 if result.get("gate_passed") is True else 1
        except (OSError, ValueError) as error:
            parser.error(str(error))
    if args.command in {"run", "check-adapter", "explore", "anomalies", "triage"}:
        from .config import load_config
        try:
            config = load_config(args.config)
            if args.command == "anomalies":
                result = _analyze(config, args.batch, args.baseline)
                print(f"anomaly_findings={len(result['findings'])} output={args.batch.resolve()}")
                return 1 if result["findings"] else 0
            if args.command == "triage":
                from .triage import triage_batch
                result = triage_batch(config, args.batch, args.output, clips=args.clips)
                print(f"signatures={result['signatures']} reproduced={result['reproduced']} "
                      f"unreproduced={result['unreproduced']} unmeasured={result['unmeasured']} "
                      f"output={args.output.resolve()}")
                return 0 if result["unreproduced"] == 0 and result["unmeasured"] == 0 else 1
            if args.command == "explore":
                from .explore import explore
                result = explore(config, args.output, max_runs=args.runs,
                                 deadline_seconds=args.deadline, seed=args.seed,
                                 tuning_noise=args.tuning_noise, steer=not args.no_steer)
                signals = _analyze(config, args.output, args.baseline)
                print(f"attempted={result['total_runs']} findings={result['findings']} "
                      f"anomaly_findings={len(signals['findings'])} output={args.output.resolve()}")
                if result["status"] == "interrupted":
                    return 130
                return 1 if result["findings"] or signals["findings"] else 0
            if args.command == "run":
                from .orchestrate import run_batch
                result = run_batch(config, args.output, workers=args.workers,
                                   seeds=args.seeds, backends=args.deciders, duration=args.seconds)
                print(f"runs={result['total_runs']} valid={result['valid_runs']} "
                      f"invalid={result['invalid_runs']} findings={result['findings']} "
                      f"output={args.output.resolve()}")
                if result["status"] == "interrupted":
                    return 130
                signals = _analyze(config, args.output)
                print(f"anomaly_findings={len(signals['findings'])}")
                return 0 if result["findings"] == 0 and not signals["findings"] else 1
            from .conformance import check_adapter
            result = check_adapter(config, args.output, seed=args.seed,
                                   map_name=args.map_name, profile=args.profile)
            for name, check in result["checks"].items():
                print(f"{name}: {check['status']} - {check['detail']}")
            print(f"status={result['status']} output={args.output.resolve()}")
            return 0 if result["status"] == "passed" else 1
        except (OSError, ValueError) as error:
            parser.error(str(error))
    if (args.decider == "tape") != (args.tape is not None):
        parser.error("--tape is required if and only if --decider is tape")
    if not args.game_command or args.game_command[0] != "--" or len(args.game_command) == 1:
        parser.error("supply the game command after --")
    try:
        instructions = args.instructions.read_text(encoding="utf-8") if args.instructions else ""
        runtime = None
        environment = None
        if args.decider == "jev":
            if args.config is None:
                raise ValueError("bridge --decider jev requires --config")
            from .config import load_config
            from .orchestrate import hosted_runtime
            config = load_config(args.config)
            if args.instructions is None:
                instructions = config.instructions
            if args.wall_timeout is None:
                args.wall_timeout = config.wall_timeout
            runtime = hosted_runtime(config, args.run_dir.resolve().parent / "decider-budget.json", 1)
            environment = dict(os.environ)
            environment.pop("TYPESAFE_API_KEY", None)
            environment.pop(config.raw.get("decider", {}).get("api_key_env", "TYPESAFE_API_KEY"), None)
        decider = make_decider(args.decider, seed=args.seed, tape_path=args.tape,
                              hosted_runtime=runtime, run_id=str(args.run_dir.resolve()))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    code = run_bridge(args.game_command[1:], args.run_dir, decider,
                      instructions=instructions, decider_name=args.decider,
                      wall_timeout_s=args.wall_timeout, env=environment)
    summary = json.loads((args.run_dir / "bridge_summary.json").read_text(encoding="utf-8"))
    print(f"decider={args.decider} exit_code={code} decisions={summary['decisions']} "
          f"run_dir={args.run_dir}", file=sys.stderr)
    return 1 if code == 0 and summary.get("valid") is not True else code
