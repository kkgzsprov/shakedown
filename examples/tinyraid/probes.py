"""Controlled input workflows shared by clean and fault benchmark runs.

These probes exercise ordinary input buttons, including combinations a tactic
policy rarely chooses. They are explicit test workflows, not autonomous bug
discovery. Input generation never reads the enabled fault.
"""

PROBES = {
    "navigate_corner": {"scenario": "corner_pocket", "description": "Navigate from a wall corner"},
    "sprint_wall": {"scenario": "thin_wall", "description": "Sprint toward a wall, then release movement"},
    "reload_fire": {"scenario": "ammo_underflow", "description": "Press reload and fire together once"},
    "heal_top_up": {"scenario": "heal_overflow", "description": "Try healing at full health once"},
    "unlock_door": {"scenario": "door_no_key", "description": "Try an adjacent locked door without a key"},
    "grenade_escape": {"scenario": "grenade", "description": "Wait for a hazard and retain reflex evasion inputs"},
    "crouched_extract": {"scenario": "extraction", "description": "Keep requesting extraction while crouched"},
}


def input_for_probe(name: str, game, adapter_inputs: dict) -> dict:
    """Return complete tick inputs without consulting fault or verdict metadata."""
    if name not in PROBES:
        raise ValueError(f"unknown tinyraid input probe: {name}")
    if name == "navigate_corner":
        return dict(adapter_inputs)
    if name == "grenade_escape":
        # The reference benchmark uses the cautious profile: sprinting movement
        # is its hazard reflex, while ordinary walking is held for this workflow.
        if adapter_inputs.get("sprint") and adapter_inputs.get("move"):
            return {"stance": "stand", "move": list(adapter_inputs["move"]), "sprint": True}
        return {"stance": "stand"}
    if name == "crouched_extract":
        return {"stance": "crouch", "extract": True}
    if name == "sprint_wall" and game.step < 30:
        return {"stance": "stand", "move": [1.0, 0.0], "sprint": True}
    if game.step == 0:
        if name == "reload_fire":
            return {"stance": "stand", "reload": True, "fire": True}
        if name == "heal_top_up":
            return {"stance": "stand", "heal": True}
        if name == "unlock_door":
            return {"stance": "stand", "use_door": True}
    return {"stance": "stand"}
