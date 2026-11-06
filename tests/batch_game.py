"""Small subprocess fixture for batch isolation, evidence, and failure handling."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shakedown.protocol import PREFIX, parse_answer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--map", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("--record-inputs", type=Path, required=True)
    parser.add_argument("--decider", required=True)
    parser.add_argument("--tuning", type=Path, required=True)
    parser.add_argument("--mode", choices=("normal", "mixed", "timeout"), default="normal")
    parser.add_argument("--label", default="fixture")
    parser.add_argument("--ok-code", type=int, default=0)
    parser.add_argument("--oracle-code", type=int, default=4)
    args = parser.parse_args()

    state_path = Path("profile.json")
    initial = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"counter": 0}
    Path("cwd-evidence.json").write_text(json.dumps({"cwd": str(Path.cwd()), "initial": initial,
                                                    "label": args.label}), encoding="utf-8")
    if initial["counter"] != 0:
        return 9
    state_path.write_text(json.dumps({"counter": 1, "seed": args.seed}), encoding="utf-8")
    if args.mode == "timeout":
        time.sleep(60)
    if args.mode == "mixed" and args.seed == 2:
        return 7
    if args.mode == "mixed" and args.seed == 6:
        return 0
    if args.mode == "mixed" and args.seed == 7:
        args.telemetry.write_text("{not-json\n", encoding="utf-8")
        return 0

    rows = [{"step": 0, "t": 0.0, "type": "run_begin", "seed": args.seed,
             "map": args.map, "profile": args.profile}]
    if args.decider == "bridge":
        hello = {"v": 1, "type": "hello", "game": "batch_fixture", "adapter_version": "1",
                 "seed": args.seed, "profile": args.profile}
        print(PREFIX + json.dumps(hello), flush=True)
    for seq in (1, 2):
        request = {"v": 1, "type": "decide", "seq": seq, "step": seq, "reason": "timer",
                   "profile": args.profile, "state": {"seed": args.seed, "tick": seq},
                   "candidates": {"advance": "Advance", "hold": "Wait"}, "order_seed": seq}
        if args.decider == "bridge":
            print(PREFIX + json.dumps(request), flush=True)
            choice = parse_answer(sys.stdin.readline(), request)["choice"]
        else:
            choice = "advance"
        rows.append({"step": seq, "t": seq / 60, "type": "decision", "seq": seq,
                     "choice": choice, "reason": "timer", "status": "selected"})
    oracle = "fixture_stuck" if args.mode == "mixed" and args.seed == 3 else None
    if oracle:
        rows.append({"step": 3, "t": .05, "type": "oracle", "oracle": oracle})
    outcome = "oracle" if oracle else "extract"
    rows.append({"step": 3, "t": .05, "type": "run_end", "outcome": outcome,
                 "oracle": oracle, "counts": {"shots": 2, "hits": 1, "loot": args.seed},
                 "hp": 100, "loot": args.seed, "distance_walked": 1.0, "x": 1, "y": 1})
    args.telemetry.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    args.record_inputs.write_text(json.dumps({"v": 1, "game": "batch_fixture", "inputs": [],
                                             "result": {"step": 3, "outcome": outcome}}), encoding="utf-8")
    if args.decider == "bridge":
        print(PREFIX + json.dumps({"v": 1, "type": "end", "step": 3, "outcome": outcome,
                                   **({"oracle": oracle} if oracle else {})}), flush=True)
    print("ordinary fixture output", flush=True)
    if args.mode == "mixed":
        return {3: args.oracle_code, 4: 2, 5: 5}.get(args.seed, args.ok_code)
    return args.ok_code


if __name__ == "__main__":
    sys.exit(main())
