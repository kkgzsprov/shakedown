"""Apply a reviewed defaults proposal to the documented tuning key table.

Verification re-derives the patch from the source table and tuning values, so a
stale, inconsistent, or already-applied receipt is refused before any write.
Nothing here builds, tests, rolls back, or commits; those stay operator steps.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path, PureWindowsPath

from .optimize import defaults
from .orchestrate import digest, write_json
from .planner.engine import read_json
from .proposals import _rebuild, _unified


def verify_proposal(config, proposal_dir) -> dict:
    """Check one proposal directory against the live table without writing."""
    proposal_dir = Path(proposal_dir).resolve()
    proposal = read_json(proposal_dir / "proposal.json")
    tuning = read_json(proposal_dir / "tuning.json")
    patch_bytes = (proposal_dir / "defaults.patch").read_bytes()
    if (type(proposal) is not dict or proposal.get("v") != 1
            or proposal.get("status") != "proposed" or proposal.get("applied") is not False
            or type(proposal.get("gate")) is not dict
            or proposal["gate"].get("status") != "passed"
            or proposal["gate"].get("proposed") is not True):
        raise ValueError("proposal must be an unapplied receipt from a passed holdout gate")
    if (type(tuning) is not dict or tuning.get("tuning_version") != 1
            or type(tuning.get("values")) is not dict):
        raise ValueError("proposal tuning values are invalid")
    candidate = tuning["values"]
    if digest(candidate) != proposal["gate"].get("candidate_hash"):
        raise ValueError("tuning values do not match the gated candidate")
    name = proposal.get("source")
    if type(name) is not str or not name or "\x00" in name:
        raise ValueError("proposal source path is invalid")
    relative = Path(name)
    if relative.is_absolute() or PureWindowsPath(name).drive or ".." in relative.parts:
        raise ValueError("default patch target must be inside the project root")
    source = (config.root / relative).resolve()
    if not source.is_relative_to(config.root):
        raise ValueError("default patch target must be inside the project root")
    keys = config.raw.get("tuning", {}).get("keys")
    if type(keys) is not str or (config.path.parent / keys).resolve() != source:
        raise ValueError("proposal target is not the configured tuning key table")
    original_bytes = source.read_bytes()
    if hashlib.sha256(original_bytes).hexdigest() != proposal.get("source_sha256"):
        raise ValueError("default source changed since the proposal; regenerate it")
    original = original_bytes.decode("utf-8-sig")
    document = read_json(source)
    if digest(defaults(document)) != proposal["gate"].get("baseline_hash"):
        raise ValueError("holdout gate does not match the documented baseline")
    changes, replacement = _rebuild(document, candidate)
    if changes != proposal.get("changes"):
        raise ValueError("recomputed changes do not match the proposal receipt")
    if _unified(original, replacement, relative.as_posix()).encode("utf-8") != patch_bytes:
        raise ValueError("defaults.patch does not match the verified proposal contents")
    return {"source": source, "relative": relative.as_posix(), "changes": changes,
            "replacement": replacement, "proposal": proposal, "proposal_dir": proposal_dir,
            "original_sha256": hashlib.sha256(original_bytes).hexdigest()}


def apply_proposal(config, proposal_dir, *, approved: bool = False) -> dict:
    """Verify a proposal, then write its defaults only under explicit approval."""
    verified = verify_proposal(config, proposal_dir)
    result = {"status": "verified", "source": verified["relative"],
              "changes": verified["changes"], "source_sha256": verified["original_sha256"]}
    if approved is not True:
        return result
    temporary = verified["source"].with_suffix(verified["source"].suffix + ".tmp")
    temporary.write_text(verified["replacement"], encoding="utf-8", newline="\n")
    temporary.replace(verified["source"])
    applied_sha256 = hashlib.sha256(verified["source"].read_bytes()).hexdigest()
    proposal = verified["proposal"]
    proposal["applied"] = True
    proposal["application"] = {"applied_at": datetime.now(timezone.utc).isoformat(),
                               "before_sha256": result["source_sha256"],
                               "after_sha256": applied_sha256,
                               "changes": verified["changes"]}
    write_json(verified["proposal_dir"] / "proposal.json", proposal)
    return {**result, "status": "applied", "applied_sha256": applied_sha256}
