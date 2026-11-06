"""Fixed offline planted-target qualification, not a human balance verdict."""

from __future__ import annotations

import json
from pathlib import Path

from .campaign import run_balance_campaign
from .config import load_config
from .orchestrate import digest, write_json
from .planner.engine import approve_plan, read_json


def bench_balance(output_dir: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    example = root / "examples/tinyraid"
    seed_path = example / "m5-seeds.json"
    seeds = read_json(seed_path)
    if (type(seeds) is not dict or seeds.get("v") != 1
            or any(type(seeds.get(split)) is not list or not seeds[split]
                   or any(type(seed) is not int for seed in seeds[split])
                   or len(set(seeds[split])) != len(seeds[split]) for split in ("search", "holdout"))
            or set(seeds["search"]) & set(seeds["holdout"])):
        raise ValueError("invalid planted-target seed manifest")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    raw = (example / "shakedown.toml").read_text(encoding="utf-8")
    raw = raw.replace('cwd = "../.."', "cwd = " + json.dumps(str(root)))
    raw = raw.replace('keys = "tuning_keys.json"', "keys = " + json.dumps(str(example / "tuning_keys.json")))
    raw = raw.replace('maps = ["yard", "warehouse"]', 'maps = ["yard"]')
    raw = raw.replace('profiles = ["cautious", "aggressive", "looter"]', 'profiles = ["cautious"]')
    raw = raw.replace("seeds = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]", "seeds = " + json.dumps(seeds["search"]))
    raw = raw.replace("seconds = 12", "seconds = 30")
    raw += ('\n[balance]\nmethod = "grid"\ncandidates_per_round = 1\ngrid_points = 3\n'
            'policies = ["rule", "mock"]\nmin_runs = 6\nmin_gain = 0.2\ntop_k = 1\nmax_runs = 120\n'
            + "holdout_seeds = " + json.dumps(seeds["holdout"]) + "\n"
            + "policy_files = " + json.dumps([f"examples/tinyraid/{name}" for name in
                  ("__main__.py", "game.py", "adapter.py", "probes.py", "__init__.py")]) + "\n")
    config_path = output_dir / "shakedown.toml"
    config_path.write_text(raw, encoding="utf-8")
    config = load_config(config_path)
    targets = {"schema_version": 1, "targets": [
        {"metric": "loot", "profile": "cautious", "map": "yard", "min": 55, "max": 60,
         "rationale": "Planted primary-rule mechanism target; mock independently checks preference reversal."}],
        "notes": "Synthetic qualification assertion; not human-approved game balance."}
    campaign = {"schema_version": 1, "hypotheses": ["Player speed changes loot reached before extraction/timeout."],
        "experiments": [{"id": "speed", "hypothesis": "Search only player.speed while damage/vision remain fixed.",
             "profiles": ["cautious"], "maps": ["yard"], "seeds": seeds["search"],
             "tuning_ranges": {"player.speed": {"min": 1., "max": 5.}}}],
        "profile_instructions": {"cautious": "Use the same cautious policy for every tuning candidate."},
        "search_ranges": {"player.speed": {"min": 1., "max": 5.}},
        "batch_size": len(seeds["search"]), "rounds": 2,
        "stop_conditions": ["Stop after two grid candidates and one frozen holdout evaluation."]}
    target_path, campaign_path = output_dir / "targets.json", output_dir / "campaign.json"
    target_receipt, campaign_receipt = output_dir / "targets-approved.json", output_dir / "campaign-approved.json"
    write_json(target_path, targets)
    write_json(campaign_path, campaign)
    approve_plan(config, "targets", target_path, target_receipt, approved=True)
    approve_plan(config, "campaign", campaign_path, campaign_receipt, approved=True,
                 targets=target_path, targets_approval=target_receipt)
    result = run_balance_campaign(config, campaign_path, campaign_receipt, output_dir / "campaign",
        approved=True, targets=target_path, targets_approval=target_receipt)
    recovered = []
    for gate in result["gates"]:
        primary = [row for row in gate["targets"]["candidate"] if row["policy"] == "rule"]
        if gate["status"] == "passed" and primary and all(row["distance"] == 0 for row in primary):
            recovered.append(gate)
    summary = {"v": 1, "status": result["status"], "gate_passed": bool(recovered),
        "seed_manifest_hash": digest(seeds), "runs": result["attempted_runs"],
        "rounds": len(result["rounds"]), "gates": result["gates"], "proposals": result["proposals"],
        "qualification": "primary rule policy recovers planted target; mock checks preference reversal, not range attainment; human/hosted-model play unmeasured"}
    write_json(output_dir / "benchmark.json", summary)
    return summary
