"""Run a Shakedown command without a shell or changing the caller's directory."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


COMMANDS = ("bridge", "check-adapter", "run", "explore", "anomalies", "triage",
            "campaign", "plan", "approve", "bench")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, help="source checkout when the skill was copied elsewhere")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    root = args.repo.resolve() if args.repo else Path(__file__).resolve().parents[3]
    environment = dict(os.environ)
    if (root / "src" / "shakedown").is_dir():
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(root / "src"), str(root), environment.get("PYTHONPATH", ""))))
    result = subprocess.run([sys.executable, "-m", "shakedown", args.command, *args.arguments],
                            env=environment, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
