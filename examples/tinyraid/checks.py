"""Game-owned visibility fixture used by ``shakedown check-adapter``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from .adapter import Adapter
from .game import Game


def check_visibility(bug: str | None = None) -> dict:
    hidden = Game(scenario="vision", bug=bug, duration=1)
    adapter = Adapter(hidden)
    leaked = set()
    for _ in range(60):
        leaked.update(item["id"] for item in adapter.observe()["seen"])
        hidden.tick({"aim": [13.5, 5.5]})
    # A checker that suppresses every observation must not pass this fixture.
    visible = Game(scenario="vision", duration=1)
    visible.walls = []
    visible.door["open"] = True
    positive = Adapter(visible).observe()["seen"]
    passed = not leaked and any(item["id"] == "g1" for item in positive)
    return {"passed": passed, "checked_steps": 60,
            "leaked_ids": sorted(leaked), "positive_control_seen": bool(positive)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check tinyraid's game-owned observation boundary")
    parser.add_argument("--bug", choices=("vision_leak",))
    result = check_visibility(parser.parse_args(argv).bug)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
