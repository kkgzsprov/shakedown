"""Run the reference game from a checkout with no installation or network."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

# The example is distributed as source alongside the package, like tests/fake_game.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from shakedown.deciders.tape import TapeDivergence
from shakedown.protocol import ProtocolError, canonical_json

from .adapter import Adapter, PROFILES
from .game import Game


BUGS = ("corner_pocket", "thin_wall", "ammo_underflow", "door_no_key", "heal_overflow",
        "silent_grenade", "extract_crouch", "vision_leak", "wallclock_load")
SCENARIOS = ("standard", "stuck", "corner_pocket", "thin_wall", "ammo_underflow", "heal_overflow",
             "door_no_key", "grenade", "extraction", "vision")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def events_hash(events: list[dict]) -> str:
    return hashlib.sha256(canonical_json([item for item in events
                                          if item["type"] != "decision"])).hexdigest()


def validate_input(value) -> dict:
    if type(value) is not dict:
        raise ValueError("input must be an object")
    buttons = {"fire", "reload", "heal", "loot", "use_door", "extract", "sprint"}
    if set(value) - buttons - {"move", "aim", "stance"}:
        raise ValueError("unknown input field")
    for key, item in value.items():
        if key in buttons and type(item) is not bool:
            raise ValueError(f"input {key} must be boolean")
        if key in {"move", "aim"} and (
            type(item) is not list or len(item) != 2 or any(
                type(number) not in (int, float) or not math.isfinite(number) for number in item)
        ):
            raise ValueError(f"input {key} must contain two finite numbers")
        if key == "move" and math.hypot(*item) > 1.000001:
            raise ValueError("movement input exceeds unit length")
        if key == "stance" and item not in {"stand", "crouch"}:
            raise ValueError("invalid stance")
    return value


def read_recording(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (type(data) is not dict or type(data.get("v")) is not int
            or data["v"] != 1 or data.get("game") != "tinyraid"):
        raise ValueError("not a tinyraid v1 input recording")
    config, inputs, result = data.get("config"), data.get("inputs"), data.get("result")
    if type(config) is not dict or set(config) != {
        "seed", "map_name", "duration", "bug", "scenario", "tuning", "profile"
    }:
        raise ValueError("invalid recording config")
    if type(inputs) is not list or type(result) is not dict or type(result.get("step")) is not int:
        raise ValueError("invalid input recording")
    if len(inputs) != result["step"]:
        raise ValueError("recording does not contain every step")
    if result.get("outcome") not in {"death", "extract", "timeout", "oracle"}:
        raise ValueError("recording has no completed outcome")
    if type(result.get("events_hash")) is not str:
        raise ValueError("recording has no event digest")
    for step, row in enumerate(inputs):
        if type(row) is not dict or type(row.get("step")) is not int or row["step"] != step:
            raise ValueError("input steps must be contiguous from zero")
        if type(row.get("t")) not in (int, float) or row["t"] != step / 60:
            raise ValueError("input timestamp does not match its exact step")
        validate_input(row.get("input"))
    validate_config(config)
    return data


def validate_config(config: dict) -> None:
    if type(config["seed"]) is not int or config["map_name"] not in {"yard", "warehouse"}:
        raise ValueError("invalid seed or map")
    duration = config["duration"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 3600:
        raise ValueError("duration must be finite and in (0, 3600]")
    if config["profile"] not in PROFILES or config["scenario"] not in SCENARIOS:
        raise ValueError("invalid profile or scenario")
    if config["bug"] is not None and config["bug"] not in BUGS:
        raise ValueError("unknown bug")
    if type(config["tuning"]) is not dict:
        raise ValueError("tuning must be an object")


def run(args) -> int:
    replay = read_recording(args.replay_inputs) if args.replay_inputs else None
    if replay:
        config = replay["config"]
    else:
        tuning = json.loads(args.tuning.read_text(encoding="utf-8")) if args.tuning else {}
        if "values" in tuning:
            if tuning.get("tuning_version") != 1 or set(tuning) != {"tuning_version", "values"}:
                raise ValueError("invalid tuning envelope")
            tuning = tuning["values"]
        config = {"seed": args.seed, "map_name": args.map, "duration": args.duration,
                  "bug": args.bug, "scenario": args.scenario, "tuning": tuning,
                  "profile": args.profile_name}
        validate_config(config)
    game = Game(**{key: value for key, value in config.items() if key != "profile"})
    adapter = None if replay else Adapter(game, config["profile"], args.decider)
    inputs = []
    code = 0
    try:
        if adapter:
            adapter.hello()
        while game.outcome is None:
            if replay:
                if game.step >= len(replay["inputs"]):
                    raise ValueError("input recording ended before the game")
                event_input = replay["inputs"][game.step]["input"]
            else:
                event_input = adapter.tick()
            row = {"step": game.step, "t": game.time, "input": event_input}
            inputs.append(row)
            game.tick(event_input)
            if args.frame_dir and game.step % args.frame_every == 0:
                from .render import write_frame
                args.frame_dir.mkdir(parents=True, exist_ok=True)
                write_frame(game, args.frame_dir / f"{game.step:07d}.png")
        if adapter:
            adapter.close()
        if replay and (len(inputs) != len(replay["inputs"]) or
                       events_hash(game.events) != replay["result"]["events_hash"] or
                       game.outcome != replay["result"]["outcome"] or
                       game.oracle != replay["result"].get("oracle")):
            print("input replay assertion failed: game events differ", file=sys.stderr)
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
            args.telemetry.write_bytes(b"".join(canonical_json(event) + b"\n" for event in game.events))
        if args.record_inputs:
            write_json(args.record_inputs, {"v": 1, "game": "tinyraid", "config": config,
                                           "inputs": inputs, "result": {
                                               "outcome": game.outcome, "oracle": game.oracle,
                                               "step": game.step, "events_hash": events_hash(game.events)}})
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic headless tinyraid reference game")
    parser.add_argument("--headless", action="store_true", help="accepted; headless is the default")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--map", choices=("yard", "warehouse"), default="yard")
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--profile-name", choices=tuple(PROFILES), default="cautious")
    parser.add_argument("--decider", default="rule", help="rule, bridge, or tape:<file>")
    parser.add_argument("--telemetry", type=Path)
    parser.add_argument("--record-inputs", type=Path)
    parser.add_argument("--replay-inputs", type=Path)
    parser.add_argument("--bug", choices=BUGS)
    parser.add_argument("--scenario", choices=SCENARIOS, default="standard")
    parser.add_argument("--tuning", type=Path)
    parser.add_argument("--frame-dir", type=Path)
    parser.add_argument("--frame-every", type=int, default=60)
    args = parser.parse_args(argv)
    if args.decider not in {"rule", "bridge"} and not (args.decider.startswith("tape:") and args.decider[5:]):
        parser.error("decider must be rule, bridge, or tape:<file>")
    if args.frame_every <= 0:
        parser.error("frame-every must be positive")
    try:
        return run(args)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f"tinyraid configuration/recording error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
