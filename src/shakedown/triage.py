"""Persistent finding signatures backed by independent reproduction evidence."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

from .aggregate import _inventory, _read_run, _read_telemetry
from .config import ProjectConfig, build_command
from .orchestrate import digest, run_case, write_json
from .protocol import canonical_json


def _reject(value):
    raise ValueError(f"nonfinite JSON constant: {value}")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject)


def _lines(path: Path) -> list[dict]:
    rows = [json.loads(line, parse_constant=_reject)
            for line in path.read_text(encoding="utf-8").splitlines()]
    if any(type(row) is not dict for row in rows):
        raise ValueError(f"expected objects in {path.name}")
    return rows


def _failure_class(metadata: dict) -> str | None:
    reasons = metadata.get("invalid_reasons", [])
    if type(reasons) is not list:
        reasons = []
    for reason in ("cancelled", "wall_timeout", "contract_error", "divergence", "crash"):
        if reason in reasons:
            return reason
    return "invalid_evidence" if metadata.get("valid") is not True else None


def finding_signature(metadata: dict, rows: list[dict], *, signal: str | None = None,
                      group: dict | None = None) -> dict:
    """Normalize evidence, without using the game's planted-fault switches."""
    ending = rows[-1] if rows else metadata.get("result")
    if type(ending) is not dict:
        ending = {}
    if signal is not None:
        group = group or {}
        return {"mechanism": "anomaly", "signal": signal,
                "map": group.get("map", metadata.get("map")),
                "profile": group.get("profile", metadata.get("profile")),
                "variant": group.get("variant", metadata.get("variant", "default"))}
    oracle = ending.get("oracle")
    event = next((row for row in reversed(rows)
                  if row.get("type") == "oracle" and row.get("oracle") == oracle), ending)
    detail = str(event.get("detail", ""))
    region = []
    for coordinate in ("x", "y"):
        value = event.get(coordinate, ending.get(coordinate))
        try:
            coordinate = round(value / 25) * 25 if type(value) in (int, float) and math.isfinite(value) else None
        except OverflowError:
            coordinate = None
        region.append(coordinate)
    return {"mechanism": "oracle" if oracle else "execution",
            "outcome": _failure_class(metadata) or ending.get("outcome", "unknown"),
            "oracle": oracle, "map": metadata.get("map"),
            "detail": re.sub(r"\d", "", detail), "region": region}


def _evidence_hash(folder: Path) -> str:
    hasher = hashlib.sha256()
    for name in ("run.json", "telemetry.jsonl", "inputs.json", "decision_tape.jsonl", "stdout.log"):
        path = folder / name
        hasher.update(name.encode() + b"\0")
        hasher.update(path.read_bytes() if path.is_file() else b"<missing>")
        hasher.update(b"\0")
    return hasher.hexdigest()


def _proof_files(folder: Path, reproduction: dict) -> dict[str, str]:
    names = ["bug_draft.json", "reproduction_receipt.json"]
    for case in reproduction.get("evidence", []):
        for name in ("run.json", "telemetry.jsonl", "inputs.json", "decision_tape.jsonl",
                     "bridge_summary.json", "stdout.log"):
            if (folder / case / name).is_file():
                names.append(f"{case}/{name}")
    if reproduction.get("status") == "reproduced":
        for case in reproduction.get("evidence", []):
            required = ["run.json", "stdout.log"]
            if reproduction.get("input_match") is True:
                required += ["telemetry.jsonl", "inputs.json"]
            for name in required:
                if not (folder / case / name).is_file():
                    raise ValueError(f"required reproduction proof is missing: {case}/{name}")
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in names}


def _validate_proof(catalog: Path, entry: dict) -> None:
    """A saved verdict is historical until its retained evidence is checked."""
    folder = catalog / entry["id"]
    files = entry.get("proof_files")
    issues = []
    if (type(files) is not dict or not {"bug_draft.json", "reproduction_receipt.json"} <= set(files)):
        issues.append("stored reproduction proof inventory is missing")
    else:
        for name, expected in files.items():
            path = Path(name)
            if (type(name) is not str or path.is_absolute() or ".." in path.parts
                    or type(expected) is not str):
                raise ValueError("invalid stored reproduction proof path")
            try:
                if hashlib.sha256((folder / path).read_bytes()).hexdigest() != expected:
                    issues.append(f"stored proof changed: {name}")
            except OSError:
                issues.append(f"stored proof missing: {name}")
        if not issues:
            try:
                receipt = _load(folder / "reproduction_receipt.json")
                historical = entry.get("historical_reproduction", entry["reproduction"])
                if (type(receipt) is not dict or receipt.get("signature") != entry["signature"]
                        or receipt.get("reproduction") != historical):
                    issues.append("stored receipt disagrees with the catalogue")
                elif historical.get("status") == "reproduced":
                    for case in historical.get("evidence", []):
                        if case not in {"reproduction", "input-replay"}:
                            raise ValueError("invalid reproduction evidence case")
                        required = ["run.json", "stdout.log"]
                        if historical.get("input_match") is True:
                            required += ["telemetry.jsonl", "inputs.json"]
                        if any(f"{case}/{name}" not in files for name in required):
                            issues.append(f"stored proof inventory omits required evidence: {case}")
            except (OSError, ValueError, TypeError, KeyError):
                issues.append("stored reproduction receipt is invalid")
    if issues:
        entry.setdefault("historical_status", entry["status"])
        entry.setdefault("historical_reproduction", entry.get("reproduction"))
        entry["status"] = "unmeasured"
        entry["proof_issues"] = issues
        entry["reproduction"] = {"status": "unmeasured", "detail": "; ".join(issues),
                                 "evidence": [], "policy_match": None, "input_match": None}


def _same_end(first: list[dict], second: list[dict]) -> bool:
    return bool(first and second and first[-1].get("outcome") == second[-1].get("outcome")
                and first[-1].get("oracle") == second[-1].get("oracle"))


def _game_events(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["type"] != "decision"]


def _signal_matches(signal: str, metadata: dict, rows: list[dict]) -> bool:
    from .anomalies import signal_observation
    return signal_observation(signal, metadata, rows) is True


def _source(config: ProjectConfig, batch_dir: Path, run_id: str,
            planned: dict, claimed: dict | None = None) -> tuple[dict, list[dict], list[str]]:
    folder = batch_dir / "runs" / run_id
    errors = []
    try:
        metadata = _load(folder / "run.json")
        if type(metadata) is not dict:
            raise ValueError("run metadata is not an object")
    except (OSError, ValueError, TypeError) as error:
        return dict(claimed or planned), [], [f"missing or invalid run metadata: {error}"]
    if any(metadata.get(key) != planned.get(key)
           for key in ("id", "seed", "map", "profile", "decider", "tuning_hash")):
        errors.append("source differs from submitted inventory")
    runtime_fields = ("extra_args", "seconds", "tuning", "variant", "noise_keys")
    if any(key in planned and metadata.get(key) != planned[key] for key in runtime_fields):
        errors.append("source runtime overrides differ from the submitted inventory")
    if metadata.get("config_hash") != hashlib.sha256(config.path.read_bytes()).hexdigest():
        errors.append("source configuration hash differs from the supplied configuration")
    if claimed is not None and any(key in claimed and claimed[key] != metadata.get(key)
                                  for key in ("id", "exit_code", "valid", "status", "result", *runtime_fields)):
        errors.append("finding differs from source run metadata")
    extra = metadata.get("extra_args", [])
    if type(extra) is not list or any(type(value) is not str or "\x00" in value for value in extra):
        errors.append("invalid source extra arguments")
    if type(metadata.get("command")) is not list or not metadata.get("command"):
        errors.append("source has no executed command")
    elif not errors:
        try:
            mode = metadata.get("mode")
            replay = mode == "inputs"
            if replay:
                inputs = metadata.get("replay_inputs")
                if type(inputs) is not str or not Path(inputs).is_absolute():
                    raise ValueError("input-replay source has no original recording path")
            else:
                inputs = None
                expected_mode = "bridge" if metadata["decider"] in {"random", "mock", "jev"} else "rule"
                if metadata["decider"] == "tape":
                    if type(mode) is not str or not mode.startswith("tape:") or not Path(mode[5:]).is_absolute():
                        raise ValueError("source tape mode has no absolute tape path")
                    expected_mode = mode
                if mode != expected_mode:
                    raise ValueError("source mode differs from the recorded decider")
            expected = build_command(config, run_dir=folder, seed=metadata["seed"],
                                     map_name=metadata["map"], profile=metadata["profile"],
                                     decider_mode=mode, duration=metadata["seconds"],
                                     tuning_file=folder / "tuning.json", inputs_file=Path(inputs) if inputs else None,
                                     replay=replay, extra_args=tuple(extra))
            if metadata["command"] != expected:
                raise ValueError("source command differs from its recorded runtime arguments")
            cwd = metadata.get("cwd")
            if type(cwd) is not str or not Path(cwd).is_absolute() or Path(cwd).resolve() != (folder / "work").resolve():
                raise ValueError("source working directory differs from its isolated run directory")
            if _load(folder / "tuning.json") != {"tuning_version": 1, "values": metadata["tuning"]}:
                raise ValueError("source tuning artifact differs from its recorded values")
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
            errors.append(f"source command provenance is invalid: {error}")
    # Independently validate healthy game observations. Expected crash/contract
    # failures may have only partial events, but cannot become healthy findings.
    validated, _, issues = _read_run(folder)
    rows = []
    if metadata.get("valid") is True:
        errors.extend(issues)
        if not issues and validated is not None:
            rows = _read_telemetry(folder / "telemetry.jsonl", metadata)
    elif (folder / "telemetry.jsonl").is_file():
        try:
            rows = _lines(folder / "telemetry.jsonl")
        except (OSError, ValueError, TypeError):
            pass
    if metadata.get("valid") is not True and not (folder / "stdout.log").is_file():
        errors.append("execution failure has no process log")
    return metadata, rows, errors


def _reproduce(config: ProjectConfig, folder: Path, metadata: dict, rows: list[dict],
               source_dir: Path, signal: str | None, errors: list[str]) -> dict:
    if errors:
        return {"status": "unmeasured", "detail": "; ".join(errors), "evidence": []}
    failure = _failure_class(metadata)
    if signal is not None and not _signal_matches(signal, metadata, rows):
        return {"status": "unmeasured", "detail": "source does not support the anomaly signal", "evidence": []}
    if failure in {"cancelled", "invalid_evidence"}:
        return {"status": "unmeasured", "detail": "no executable game failure to reproduce", "evidence": []}
    if failure == "contract_error":
        # Exit 2 also covers ordinary argument/tuning mistakes. Without an
        # explicit protocol receipt, reproducing the same code proves no wire bug.
        try:
            summary = _load(source_dir / "bridge_summary.json")
            if type(summary) is not dict or type(summary.get("contract_error")) is not str:
                raise ValueError("missing bridge contract receipt")
        except (OSError, ValueError, TypeError):
            return {"status": "unmeasured", "detail": "contract exit has no protocol violation receipt", "evidence": []}
    if failure is None and (not rows or rows[-1].get("outcome") != "oracle") and signal is None:
        return {"status": "unmeasured", "detail": "source is not a finding", "evidence": []}
    if failure is None and (config.replay_command is None or not (source_dir / "inputs.json").is_file()):
        return {"status": "unmeasured", "detail": "independent input replay is unavailable", "evidence": []}
    arguments = tuple(metadata.get("extra_args", []))
    parameters = {"seed": metadata["seed"], "map_name": metadata["map"],
                  "profile": metadata["profile"], "duration": metadata["seconds"],
                  "tuning": metadata["tuning"]}
    decider = metadata["decider"]
    options = {"decider": "rule"}
    if decider in {"mock", "random", "tape", "jev"}:
        tape = source_dir / "decision_tape.jsonl"
        if decider == "tape" and not tape.is_file() and str(metadata.get("mode", "")).startswith("tape:"):
            tape = Path(metadata["mode"][5:])
        if not tape.is_file() or not tape.stat().st_size:
            return {"status": "unmeasured", "detail": "the original decision tape is missing", "evidence": []}
        options = {"decider": "tape", "tape_path": tape}
    first = run_case(config, folder / "reproduction", **parameters, **options, extra_args=arguments)
    proof = {"status": "unreproduced", "detail": "reproduction did not match the original finding",
             "evidence": ["reproduction"], "command": first.get("command"), "cwd": first.get("cwd"),
             "exit_code": first.get("exit_code"), "policy_match": False, "input_match": None}
    if failure is not None:
        # A launch/configuration/evidence error is not a reproduced game crash.
        forbidden = {"execution_error", "launch_error", "classification_error"}
        matched = (_failure_class(first) == failure and first.get("exit_code") == metadata.get("exit_code")
                   and not forbidden.intersection(first.get("invalid_reasons", [])))
        if failure == "wall_timeout":
            matched = first.get("wall_timeout") is True
        proof.update(status="reproduced" if matched else "unreproduced", policy_match=matched,
                     detail="execution failure class and exit outcome matched" if matched else proof["detail"])
        return proof
    try:
        repeated = _read_telemetry(folder / "reproduction" / "telemetry.jsonl", first)
        matched = (first.get("valid") is True and _same_end(rows, repeated)
                   and (signal is None or _signal_matches(signal, first, repeated)))
        proof["policy_match"] = matched
        if not matched:
            return proof
        if config.replay_command is None or not (source_dir / "inputs.json").is_file():
            proof.update(status="unmeasured", detail="independent input replay is unavailable")
            return proof
        replay = run_case(config, folder / "input-replay", **parameters,
                          replay_inputs=source_dir / "inputs.json")
        proof["evidence"].append("input-replay")
        replayed = _read_telemetry(folder / "input-replay" / "telemetry.jsonl", replay)
        input_match = (replay.get("valid") is True and replay.get("exit_code") == metadata["exit_code"]
                       and _same_end(rows, replayed) and _game_events(rows) == _game_events(replayed)
                       and (signal is None or _signal_matches(signal, replay, replayed)))
        proof.update(status="reproduced" if input_match else "unreproduced", input_match=input_match,
                     detail="policy and independent input replay matched the finding" if input_match
                     else "independent input replay did not match the finding")
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        proof["detail"] = f"reproduction evidence is invalid: {error}"
    return proof


def triage_batch(config: ProjectConfig, batch_dir: Path, output_dir: Path, *, clips: bool = False) -> dict:
    """Add new source receipts to an idempotent catalogue; never retry implicitly."""
    batch_dir, output_dir = Path(batch_dir).resolve(), Path(output_dir).resolve()
    inventory, _ = _inventory(batch_dir)
    if inventory is None:
        raise ValueError("triage requires a submitted batch inventory")
    findings = _lines(batch_dir / "findings.jsonl")
    requests = []
    for finding in findings:
        run_id = finding.get("id")
        if run_id not in inventory:
            raise ValueError("finding run is absent from the submitted inventory")
        requests.append((run_id, None, None, finding))
    anomaly_path = batch_dir / "anomalies.json"
    if anomaly_path.exists():
        report = _load(anomaly_path)
        if type(report) is not dict or type(report.get("findings")) is not list:
            raise ValueError("invalid anomaly findings")
        for finding in report["findings"]:
            if (type(finding) is not dict or type(finding.get("signal")) is not str
                    or type(finding.get("group")) is not dict or type(finding.get("run_ids")) is not list):
                raise ValueError("invalid anomaly finding")
            for run_id in finding["run_ids"]:
                if run_id not in inventory:
                    raise ValueError("anomaly run is absent from the submitted inventory")
                requests.append((run_id, finding["signal"], finding["group"], None))
    catalog = output_dir / "findings"
    catalog.mkdir(parents=True, exist_ok=True)
    index_path = catalog / "index.json"
    if index_path.exists():
        index = _load(index_path)
        if (type(index) is not dict or type(index.get("v")) is not int or index["v"] != 1
                or type(index.get("entries")) is not list):
            raise ValueError("invalid persistent finding index")
        entries = index["entries"]
        for entry in entries:
            if (type(entry) is not dict or type(entry.get("signature")) is not dict
                    or entry.get("id") != "s" + digest(entry["signature"])[:16]
                    or type(entry.get("sources")) is not list
                    or entry.get("status") not in {"reproduced", "unreproduced", "unmeasured"}
                    or entry.get("run_count") != len(entry["sources"])
                    or any(type(source) is not dict or type(source.get("key")) is not str
                           for source in entry["sources"])
                    or len({source["key"] for source in entry["sources"]}) != len(entry["sources"])
                    or entry.get("draft") != f"{entry['id']}/bug_draft.json"):
                raise ValueError("invalid persistent signature entry")
        if len({entry["id"] for entry in entries}) != len(entries):
            raise ValueError("duplicate persistent signature entries")
        for entry in entries:
            _validate_proof(catalog, entry)
    else:
        entries = []
    by_signature = {digest(entry["signature"]): entry for entry in entries}
    new_signatures = new_sources = 0
    for run_id, signal, group, claimed in requests:
        source_dir = batch_dir / "runs" / run_id
        metadata, rows, errors = _source(config, batch_dir, run_id, inventory[run_id], claimed)
        if group is not None:
            if any(value != metadata.get(key, "default" if key == "variant" else None)
                   for key, value in group.items() if key in {"map", "profile", "variant", "decider", "tuning_hash"}):
                errors.append("anomaly group differs from its supporting run")
            try:
                if not errors and not _signal_matches(signal, metadata, rows):
                    errors.append("source does not support the anomaly signal")
            except (ValueError, TypeError, KeyError):
                errors.append("anomaly signal cannot be measured from source evidence")
        signature = finding_signature(metadata, rows, signal=signal, group=group)
        signature_hash = digest(signature)
        evidence_hash = _evidence_hash(source_dir)
        source_key = digest({"path": str(source_dir), "evidence_hash": evidence_hash})
        source = {"key": source_key, "batch": str(batch_dir), "run_id": run_id,
                  "path": str(source_dir), "evidence_hash": evidence_hash,
                  "validated": not errors, "issues": errors}
        entry = by_signature.get(signature_hash)
        if entry is not None:
            if all(item.get("key") != source_key for item in entry["sources"]):
                entry["sources"].append(source)
                entry["run_count"] = len(entry["sources"])
                new_sources += 1
            continue
        signature_id = "s" + signature_hash[:16]
        if any(item["id"] == signature_id for item in entries):
            raise ValueError("finding signature identifier collision")
        folder = catalog / signature_id
        folder.mkdir(exist_ok=False)
        reproduction = {"status": "unmeasured", "detail": "reproduction is incomplete; no automatic retry",
                        "evidence": []}
        entry = {"id": signature_id, "signature": signature, "sources": [source], "run_count": 1,
                 "status": "unmeasured", "reproduction": reproduction,
                 "draft": f"{signature_id}/bug_draft.json"}
        entries.append(entry)
        by_signature[signature_hash] = entry
        new_signatures += 1
        new_sources += 1
        # Reserve this attempt durably before launching. An interrupted replay
        # leaves an explicit incomplete receipt instead of being retried later.
        write_json(folder / "bug_draft.json", {"v": 1, "source": source,
                                               "actual": signature, "suspected_cause": None,
                                               "reproduction": reproduction})
        write_json(index_path, {"v": 1, "entries": entries})
        try:
            reproduction = _reproduce(config, folder, metadata, rows, source_dir, signal, errors)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
            reproduction = {"status": "unmeasured", "detail": str(error), "evidence": []}
        entry.update(status=reproduction["status"], reproduction=reproduction)
        draft = {"v": 1, "title": f"{signature.get('signal') or signature.get('oracle') or signature.get('outcome')} on {metadata.get('map')}",
                 "reproduction": {"command": reproduction.get("command"), "cwd": reproduction.get("cwd"),
                                  "status": reproduction["status"]},
                 "expected": "The game's invariants and documented observable behavior hold",
                 "actual": signature, "suspected_cause": None,
                 "source": source, "last_events": rows[-10:], "evidence": reproduction["evidence"]}
        write_json(folder / "bug_draft.json", draft)
        write_json(folder / "reproduction_receipt.json", {
            "v": 1, "signature": signature, "source": source, "reproduction": reproduction})
        entry["proof_files"] = _proof_files(folder, reproduction)
        if clips:
            try:
                from .clips import build_clip
                entry["clip"] = build_clip(config, source_dir, folder / "clip", seconds=10)
            except (OSError, ValueError, TypeError, ImportError) as error:
                entry["clip"] = {"status": "unmeasured", "detail": str(error)}
        # Persist each completed signature before starting another game process.
        write_json(index_path, {"v": 1, "entries": entries})
    report = {"v": 1, "signatures": len(entries), "new_signatures": new_signatures,
              "new_sources": new_sources, "entries": entries,
              **{status: sum(entry["status"] == status for entry in entries)
                 for status in ("reproduced", "unreproduced", "unmeasured")}}
    write_json(index_path, {"v": 1, "entries": entries})
    lines = ["# Findings", "", "Anomaly signals are investigation pointers; reproduction is separate evidence.",
             "", "| Signature | State | Runs | Draft |", "|---|---|---|---|"]
    for entry in entries:
        lines.append(f"| {entry['id']} | {entry['status']} | {entry['run_count']} | [draft]({entry['draft']}) |")
    (catalog / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(output_dir / "triage.json", report)
    return report
