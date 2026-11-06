"""Adapt this positive/negative visibility test to the game's real observation path."""

import json


def visibility_fixture(observe, reveal, hidden_id):
    """observe() returns player state; reveal() performs an ordinary reveal input."""
    before = observe()
    hidden_absent = hidden_id not in {row["id"] for row in before["seen"]}
    reveal()
    after = observe()
    revealed_present = hidden_id in {row["id"] for row in after["seen"]}
    return {"passed": hidden_absent and revealed_present,
            "hidden_absent": hidden_absent, "revealed_present": revealed_present}


def run_visibility_fixture(observe, reveal, hidden_id):
    result = visibility_fixture(observe, reveal, hidden_id)
    print(json.dumps(result, allow_nan=False), flush=True)
    return 0 if result["passed"] else 1
