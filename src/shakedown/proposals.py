"""Reviewable patches to documented defaults; never apply or commit them."""

from __future__ import annotations

from copy import deepcopy
import difflib
import hashlib
import json
from pathlib import Path

from .config import _tuning_value
from .optimize import defaults
from .orchestrate import digest, write_json
from .planner.engine import read_json


def write_proposal(config, candidate, output_dir, *, gate, source_sha256):
    if gate.get("status") != "passed" or gate.get("proposed") is not True:
        raise ValueError("a passed holdout gate is required before a defaults proposal")
    source = (config.path.parent / config.raw["tuning"]["keys"]).resolve()
    if not source.is_relative_to(config.root):
        raise ValueError("default patch target must be inside the project root")
    original_bytes = source.read_bytes()
    if hashlib.sha256(original_bytes).hexdigest() != source_sha256:
        raise ValueError("default source changed during the campaign")
    original = original_bytes.decode("utf-8-sig")
    document = read_json(source)
    if (gate.get("candidate_hash") != digest(candidate)
            or gate.get("baseline_hash") != digest(defaults(document))):
        raise ValueError("holdout gate does not match the candidate and documented baseline")
    changed = deepcopy(document)
    changes = []
    if set(candidate) != set(document["keys"]):
        raise ValueError("proposal must contain the complete documented tuning values")
    for key, value in sorted(candidate.items()):
        spec = document["keys"][key]
        _tuning_value(key, value, spec)
        field = "default" if "default" in spec else "baseline"
        before = spec[field]
        if value != before:
            changes.append({"key": key, "field": field, "before": before, "after": value})
            changed["keys"][key][field] = value
    if not changes:
        raise ValueError("proposal does not change documented defaults")
    replacement = json.dumps(changed, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    relative = source.relative_to(config.root).as_posix()
    patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                    for line in difflib.unified_diff(original.splitlines(keepends=True),
                    replacement.splitlines(keepends=True), fromfile=f"a/{relative}", tofile=f"b/{relative}"))
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / "tuning.json", {"tuning_version": 1, "values": candidate})
    (output_dir / "defaults.patch").write_text(patch, encoding="utf-8", newline="\n")
    receipt = {"v": 1, "status": "proposed", "applied": False, "source": relative,
               "source_sha256": source_sha256, "changes": changes, "gate": gate,
               "scope": "documented defaults; verify the game's integration before manual application"}
    write_json(output_dir / "proposal.json", receipt)
    return receipt
