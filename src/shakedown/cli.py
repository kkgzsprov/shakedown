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
    args = parser.parse_args(argv)
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
