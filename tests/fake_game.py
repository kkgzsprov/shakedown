"""A deterministic, protocol-speaking game fixture for bridge tests."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shakedown.protocol import PREFIX, ProtocolError, parse_answer


def emit(message: dict) -> None:
    sys.stdout.write(PREFIX + json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def make_request(seq: int, state: dict, profile: str, rng: random.Random) -> dict:
    player = state["self"]
    candidates = {"advance": "Move one unit forward", "hold": "Stay in place"}
    if player["ammo"] < 3:
        candidates["reload"] = "Restore ammunition"
    if player["hp"] < 10:
        candidates["heal"] = "Restore health"
    if state["seen"]:
        candidates["engage_e1"] = "Engage the visible target"
    order_seed = rng.randrange(2**31)
    names = list(candidates)
    random.Random(order_seed).shuffle(names)
    return {
        "v": 1,
        "type": "decide",
        "seq": seq,
        "step": seq * 10,
        "reason": "tactic_complete",
        "profile": profile,
        "state": state,
        "candidates": {name: candidates[name] for name in names},
        "order_seed": order_seed,
    }


def apply_choice(state: dict, choice: str) -> None:
    player = state["self"]
    if choice == "advance":
        player["x"] += 1
        player["hp"] = max(1, player["hp"] - 1)
    elif choice == "reload":
        player["ammo"] = 3
    elif choice == "heal":
        player["hp"] = min(10, player["hp"] + 3)
    elif choice == "engage_e1":
        player["ammo"] -= 1
    state["seen"] = [{"id": "e1", "range_m": max(1, 8 - player["x"])}]
    state["recent"] = [{"tactic": choice}]


def run(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    state = {"self": {"hp": 9, "ammo": 2, "x": 0},
             "seen": [{"id": "e1", "range_m": 8}], "recent": []}
    telemetry = open(args.telemetry, "w", encoding="utf-8", newline="\n") if args.telemetry else None

    def event(kind: str, step: int, **fields: object) -> None:
        if telemetry is not None:
            telemetry.write(json.dumps({"t": step, "step": step, "type": kind, **fields},
                                       separators=(",", ":")) + "\n")
            telemetry.flush()

    try:
        event("run_begin", 0, decider_mode=args.decider_mode)
        if args.decider_mode == "bridge":
            print("fake game started", flush=True)
            hello = {"v": 1, "type": "hello", "game": "fake_game",
                     "adapter_version": "0.0.1", "seed": args.seed, "profile": args.profile}
            emit(hello)
            if args.fault == "hello-twice":
                emit(hello)
        for seq in range(1, args.decisions + 1):
            request = make_request(seq, state, args.profile, rng)
            if args.decider_mode == "bridge":
                if args.fault == "garbage-request" and seq == 1:
                    sys.stdout.write(PREFIX + "{broken\n")
                    sys.stdout.flush()
                    return 0
                if args.fault == "bad-version" and seq == 1:
                    request["v"] = 2
                if args.fault == "too-many-candidates" and seq == 1:
                    request["candidates"] = {f"option_{i}": str(i) for i in range(13)}
                emit(request)
                try:
                    answer = parse_answer(sys.stdin.buffer.readline().decode("utf-8"), request)
                    choice = answer["choice"]
                except ProtocolError as exc:
                    print(f"answer contract error: {exc}", file=sys.stderr, flush=True)
                    return 2
            else:
                choice = sorted(request["candidates"])[0]
            event("decision", request["step"], seq=seq, reason=request["reason"], choice=choice)
            apply_choice(state, choice)
        if args.fault == "exit-7":
            return 7
        event("run_end", args.decisions * 10, outcome="extract")
        if args.decider_mode == "bridge" and args.fault != "no-end":
            emit({"v": 1, "type": "end", "outcome": "extract", "step": args.decisions * 10})
        return 0
    finally:
        if telemetry is not None:
            telemetry.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--profile", default="cautious")
    parser.add_argument("--decisions", type=int, default=6)
    parser.add_argument("--decider-mode", choices=("bridge", "rule"), default="bridge")
    parser.add_argument("--telemetry", type=Path)
    parser.add_argument("--fault", choices=("garbage-request", "bad-version", "too-many-candidates",
                                            "hello-twice", "no-end", "exit-7"))
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
