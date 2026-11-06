"""Headless Switchyard runner and assertion-backed ordinary-input replay."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shakedown.deciders.tape import TapeDivergence
from shakedown.protocol import ProtocolError, canonical_json

from .adapter import Adapter
from .game import Game, MAPS, PROFILES, validate_config, validate_input


def event_hash(events):
    return hashlib.sha256(canonical_json([row for row in events if row["type"] != "decision"])).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def read_recording(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if (type(data) is not dict or set(data) != {"v", "game", "config", "inputs", "result"}
            or type(data["v"]) is not int or data["v"] != 1 or data["game"] != "switchyard"):
        raise ValueError("not a switchyard v1 recording")
    validate_config(data["config"])
    inputs, result = data["inputs"], data["result"]
    if (type(inputs) is not list or type(result) is not dict
            or type(result.get("step")) is not int or len(inputs) != result["step"]
            or result.get("outcome") not in {"death", "extract", "timeout", "oracle"}
            or type(result.get("events_hash")) is not str):
        raise ValueError("recording requires every completed turn and an event digest")
    for step, row in enumerate(inputs):
        if (type(row) is not dict or set(row) != {"step", "t", "input"}
                or type(row["step"]) is not int or row["step"] != step
                or type(row["t"]) not in (int, float) or row["t"] != float(step)):
            raise ValueError("recording turns/timestamps must be contiguous from zero")
        validate_input(row["input"])
    return data


def run(args):
    replay = read_recording(args.replay_inputs) if args.replay_inputs else None
    if replay:
        config = replay["config"]
    else:
        tuning = json.loads(args.tuning.read_text(encoding="utf-8")) if args.tuning else {}
        if type(tuning) is dict and "values" in tuning:
            if set(tuning) != {"tuning_version", "values"} or type(tuning["tuning_version"]) is not int or tuning["tuning_version"] != 1:
                raise ValueError("invalid tuning envelope")
            tuning = tuning["values"]
        config = {"seed": args.seed, "map_name": args.map, "duration": args.duration,
                  "profile": args.profile_name, "scenario": args.scenario, "tuning": tuning}
    game = Game(config)
    adapter = None if replay else Adapter(game, args.decider)
    inputs, code = [], 0
    try:
        if adapter:
            adapter.hello()
        while game.outcome is None:
            if replay:
                if game.step >= len(replay["inputs"]):
                    raise ValueError("recording ended before game completion")
                event_input = replay["inputs"][game.step]["input"]
            else:
                event_input = adapter.tick()
            inputs.append({"step": game.step, "t": game.time, "input": event_input})
            game.tick(event_input)
        if adapter:
            adapter.close()
        if replay and (len(inputs) != len(replay["inputs"]) or event_hash(game.events) != replay["result"]["events_hash"]
                       or game.outcome != replay["result"]["outcome"] or game.oracle != replay["result"].get("oracle")):
            code = 3
        elif game.oracle:
            code = 4
    except TapeDivergence as error:
        print(f"tape divergence: {error}", file=sys.stderr)
        code = 5
    except (ProtocolError, UnicodeError) as error:
        print(f"answer contract error: {error}", file=sys.stderr)
        code = 2
    finally:
        if args.telemetry:
            args.telemetry.parent.mkdir(parents=True, exist_ok=True)
            args.telemetry.write_bytes(b"".join(canonical_json(row) + b"\n" for row in game.events))
        if args.record_inputs:
            write_json(args.record_inputs, {"v": 1, "game": "switchyard", "config": config, "inputs": inputs,
                "result": {"step": game.step, "outcome": game.outcome, "oracle": game.oracle,
                           "events_hash": event_hash(game.events)}})
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deterministic turn-based Switchyard route puzzle")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--map", choices=tuple(MAPS), default="depot")
    parser.add_argument("--duration", type=float, default=16)
    parser.add_argument("--profile-name", choices=PROFILES, default="courier")
    parser.add_argument("--scenario", choices=("standard", "stuck"), default="standard")
    parser.add_argument("--decider", default="rule")
    parser.add_argument("--tuning", type=Path)
    parser.add_argument("--telemetry", type=Path)
    parser.add_argument("--record-inputs", type=Path)
    parser.add_argument("--replay-inputs", type=Path)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"switchyard contract error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
