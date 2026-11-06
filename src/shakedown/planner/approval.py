"""Content-bound operator approval receipts, not identity authentication."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from ..protocol import canonical_json
from .schema import validate_output


_FIELDS = {"v", "kind", "artifact", "sha256", "approved_by", "context_hash",
           "approved", "approved_at"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject(value):
    raise ValueError(f"nonfinite JSON constant: {value}")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object,
                      parse_constant=_reject)


def _kind(kind: str) -> None:
    if type(kind) is not str or kind not in {"targets", "campaign"}:
        raise ValueError("only targets and campaign documents can be approved")


def _context(value: str | None) -> None:
    if value is not None and (type(value) is not str or _HASH.fullmatch(value) is None):
        raise ValueError("context_hash must be a SHA-256 hex digest or null")


def _artifact(kind: str, path: Path):
    _kind(kind)
    data = _load(path)
    validate_output(kind, data)
    return hashlib.sha256(canonical_json(data)).hexdigest()


def approve_document(kind: str, artifact: Path, receipt: Path, *, approved: bool = False,
                     approved_by: str = "operator", context_hash: str | None = None) -> dict:
    """Record an explicit operator assertion for this exact document and scope.

    The receipt is unsigned local data. It neither proves a human identity nor
    authorizes spending, and a model's output cannot approve itself.
    """
    if approved is not True:
        raise ValueError("explicit approval is required")
    if type(approved_by) is not str or not approved_by.strip() or "\x00" in approved_by:
        raise ValueError("approved_by must name the asserting operator")
    _context(context_hash)
    artifact, receipt = Path(artifact).resolve(), Path(receipt).resolve()
    digest = _artifact(kind, artifact)
    result = {"v": 1, "kind": kind, "artifact": str(artifact), "sha256": digest,
              "approved_by": approved_by, "context_hash": context_hash,
              "approved": True, "approved_at": datetime.now(timezone.utc).isoformat()}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical_json(result) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    return result


def verify_approval(kind: str, artifact: Path, receipt: Path, *,
                    context_hash: str | None = None) -> dict:
    """Refuse changed contents, a different artifact, or a different context."""
    _kind(kind)
    _context(context_hash)
    artifact, receipt = Path(artifact).resolve(), Path(receipt).resolve()
    result = _load(receipt)
    if (type(result) is not dict or set(result) != _FIELDS
            or type(result["v"]) is not int or result["v"] != 1
            or result["approved"] is not True or result["kind"] != kind
            or type(result["artifact"]) is not str or result["artifact"] != str(artifact)
            or type(result["sha256"]) is not str or _HASH.fullmatch(result["sha256"]) is None
            or type(result["approved_by"]) is not str or not result["approved_by"].strip()
            or "\x00" in result["approved_by"]
            or result["context_hash"] != context_hash or type(result["approved_at"]) is not str):
        raise ValueError("approval receipt does not match the artifact and context")
    _context(result["context_hash"])
    try:
        timestamp = datetime.fromisoformat(result["approved_at"])
    except ValueError as error:
        raise ValueError("approval timestamp is invalid") from error
    if timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
        raise ValueError("approval timestamp must be UTC")
    if _artifact(kind, artifact) != result["sha256"]:
        raise ValueError("approved document has changed")
    return result
