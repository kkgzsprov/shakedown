"""Command-line entry points for Shakedown."""

import argparse
import json
import math
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
    if (any(item not in {"rule", "random", "mock"} for item in values)
            or len(set(values)) != len(values)):
        raise argparse.ArgumentTypeError("deciders must be unique names: rule, random, mock")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shakedown")
    subparsers = parser.add_subparsers(dest="command", required=True)
    bridge = subparsers.add_parser("bridge", help="connect a game to a local decider")
    bridge.add_argument("--decider", choices=("random", "mock", "tape"), required=True)
    bridge.add_argument("--tape")
    bridge.add_argument("--seed", type=int, default=0)
    bridge.add_argument("--instructions", type=Path)
    bridge.add_argument("--wall-timeout", type=_positive_seconds)
    bridge.add_argument("--run-dir", type=Path, required=True)
    bridge.add_argument("game_command", nargs=argparse.REMAINDER)
    batch = subparsers.add_parser("run", help="run an isolated offline batch and aggregate its evidence")
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
    args = parser.parse_args(argv)
    if args.command in {"run", "check-adapter"}:
        from .config import load_config
        try:
            config = load_config(args.config)
            if args.command == "run":
                from .orchestrate import run_batch
                result = run_batch(config, args.output, workers=args.workers,
                                   seeds=args.seeds, backends=args.deciders, duration=args.seconds)
                print(f"runs={result['total_runs']} valid={result['valid_runs']} "
                      f"invalid={result['invalid_runs']} findings={result['findings']} "
                      f"output={args.output.resolve()}")
                if result["status"] == "interrupted":
                    return 130
                return 0 if result["findings"] == 0 else 1
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
        decider = make_decider(args.decider, seed=args.seed, tape_path=args.tape)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    code = run_bridge(args.game_command[1:], args.run_dir, decider,
                      instructions=instructions, decider_name=args.decider,
                      wall_timeout_s=args.wall_timeout)
    summary = json.loads((args.run_dir / "bridge_summary.json").read_text(encoding="utf-8"))
    print(f"decider={args.decider} exit_code={code} decisions={summary['decisions']} "
          f"run_dir={args.run_dir}", file=sys.stderr)
    return code
