"""Execute adapter contract checks and keep the evidence for every verdict."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

from .config import ProjectConfig, build_command, expand_command
from .orchestrate import child_environment, prepare_work, run_case
from .processes import start_process, terminate_process
from .protocol import canonical_json, encode_answer, parse_game_line


def _read_rows(path: Path) -> list[dict]:
    def reject_constant(value):
        raise ValueError(f"nonfinite JSON value: {value}")

    rows = [json.loads(line, parse_constant=reject_constant)
            for line in path.read_text(encoding="utf-8").splitlines()]
    if not rows or any(type(row) is not dict for row in rows):
        raise ValueError(f"missing or invalid evidence in {path.name}")
    return rows


def _telemetry(path: Path, excluded: set[str] | None = None) -> list[dict]:
    rows = _read_rows(path / "telemetry.jsonl")
    if any(type(row.get("type")) is not str for row in rows):
        raise ValueError("telemetry event has no type")
    return [row for row in rows if row["type"] not in (excluded or set())]


def _completed(metadata: dict, code: int) -> bool:
    return (metadata.get("exit_code") == code and metadata.get("valid") is True
            and metadata.get("wall_timeout") is False)


def _verdict(passed: bool, detail: str, evidence: list[str], **fields) -> dict:
    return {"status": "passed" if passed else "failed", "detail": detail,
            "evidence": evidence, **fields}


def _decision_count(rows: list[dict]) -> int:
    decisions = [row for row in rows if row["type"] == "decision"]
    selected = [row for row in decisions if row.get("status") == "selected"]
    counted = selected or decisions
    if not counted or any(type(row.get("seq")) is not int for row in counted):
        raise ValueError("decision telemetry has no sequence evidence")
    return len({row["seq"] for row in counted})


def _run_process(config: ProjectConfig, command: list[str], case_dir: Path,
                 answer_fault: str | None = None) -> dict:
    """Bound both the initial protocol read and the exit after a bad answer."""
    case_dir.mkdir()
    cwd = prepare_work(config, case_dir)
    timed_out = False
    answered = False
    later_requests = 0
    error = None
    # A file avoids waiting for EOF on a pipe inherited by a descendant after
    # the adapter exits. Polling also bounds reads before the first request.
    with (case_dir / "stdout.log").open("wb") as output, start_process(
        command, cwd=cwd, env=child_environment(config),
        stdin=subprocess.PIPE if answer_fault else subprocess.DEVNULL,
        stdout=output, stderr=subprocess.STDOUT,
    ) as process:
        try:
            if answer_fault is None:
                try:
                    process.wait(timeout=config.wall_timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_process(process)
            else:
                deadline = time.monotonic() + config.wall_timeout
                pending = b""
                with (case_dir / "stdout.log").open("rb") as transcript:
                    while True:
                        if time.monotonic() >= deadline:
                            timed_out = True
                            terminate_process(process)
                            break
                        raw = transcript.readline()
                        if not raw:
                            if process.poll() is not None:
                                break
                            time.sleep(.005)
                            continue
                        pending += raw
                        if not pending.endswith(b"\n"):
                            continue
                        message = parse_game_line(pending.decode("utf-8"))
                        pending = b""
                        if message is None or message["type"] != "decide":
                            continue
                        if answered:
                            later_requests += 1
                            continue
                        if answer_fault == "seq":
                            choice = next(iter(message["candidates"]))
                            answer = encode_answer(message["seq"] + 1, choice, "mock", {})
                        elif answer_fault == "choice":
                            choice = "__shakedown_invalid_choice__"
                            while choice in message["candidates"]:
                                choice += "_"
                            answer = encode_answer(message["seq"], choice, "mock", {})
                        else:
                            answer = "{broken\n"
                        process.stdin.write(answer.encode("utf-8"))
                        process.stdin.flush()
                        answered = True
        except (OSError, ValueError, UnicodeError) as exc:
            error = str(exc)
        finally:
            if process.poll() is None:
                terminate_process(process)
            process.wait(timeout=5)
    result = {"command": command, "cwd": str(cwd), "exit_code": process.returncode,
              "wall_timeout": timed_out, "answered": answered,
              "later_requests": later_requests, "error": error}
    (case_dir / "check_process.json").write_bytes(canonical_json(result) + b"\n")
    return result


def check_adapter(config: ProjectConfig, output_dir: Path, *, seed: int | None = None,
                  map_name: str | None = None, profile: str | None = None) -> dict:
    """Run C1-C7, distinguishing failed evidence from unavailable fixtures."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    seed = config.seeds[0] if seed is None else seed
    map_name = config.maps[0] if map_name is None else map_name
    profile = config.profiles[0] if profile is None else profile
    parameters = {"seed": seed, "map_name": map_name, "profile": profile}
    codes = config.exit_codes
    checks = {}

    def case(name: str, **overrides) -> dict:
        return run_case(config, output_dir / name, **parameters, **overrides)

    def perform(name, operation):
        try:
            checks[name] = operation()
        except (OSError, ValueError, TypeError, KeyError, OverflowError,
                subprocess.SubprocessError) as exc:
            checks[name] = {"status": "failed", "detail": str(exc), "evidence": []}

    def c1():
        first, second = case("c1-first"), case("c1-second")
        same = all((output_dir / "c1-first" / filename).read_bytes()
                   == (output_dir / "c1-second" / filename).read_bytes()
                   for filename in ("telemetry.jsonl", "inputs.json"))
        passed = _completed(first, codes["ok"]) and _completed(second, codes["ok"]) and same
        return _verdict(passed, "Rule runs must have identical telemetry and input bytes",
                        ["c1-first", "c1-second"], identical=same)

    def c2():
        if config.replay_command is None:
            return {"status": "unmeasured", "detail": "game.replay_command is not configured",
                    "evidence": []}
        source = output_dir / "c1-first"
        if not (source / "inputs.json").is_file():
            return _verdict(False, "The original input recording is missing", ["c1-first"])
        replay = case("c2-replay", replay_inputs=source / "inputs.json")
        original = _telemetry(source, {"decision"})
        repeated = _telemetry(output_dir / "c2-replay", {"decision"})
        return _verdict(_completed(replay, codes["ok"]) and original == repeated,
                        "Input replay must reproduce all game events without a decider",
                        ["c1-first", "c2-replay"], identical=original == repeated)

    def c3():
        original = case("c3-original", decider="mock")
        replay = case("c3-replay", decider="tape",
                      tape_path=output_dir / "c3-original" / "decision_tape.jsonl")
        first = _telemetry(output_dir / "c3-original", {"run_begin"})
        second = _telemetry(output_dir / "c3-replay", {"run_begin"})
        tape = _read_rows(output_dir / "c3-original" / "decision_tape.jsonl")
        counts = [_decision_count(first), _decision_count(second), len(tape)]
        passed = (_completed(original, codes["ok"]) and _completed(replay, codes["ok"])
                  and first == second and len(set(counts)) == 1)
        return _verdict(passed, "Tape replay must match telemetry and decision count",
                        ["c3-original", "c3-replay"], identical=first == second,
                        decision_counts=counts)

    def direct_command(name: str, *, template=None) -> list[str]:
        directory = output_dir / name
        tuning_file = output_dir / f"{name}-tuning.json"
        tuning_file.write_bytes(canonical_json({"tuning_version": 1, "values": {}}) + b"\n")
        values = {"run_dir": directory, **parameters, "decider_mode": "bridge",
                  "duration": config.duration, "tuning_file": tuning_file}
        return (build_command(config, **values) if template is None
                else expand_command(config, template, **values))

    def c4():
        outcomes = {}
        for fault in ("seq", "choice", "json"):
            name = f"c4-{fault}"
            result = _run_process(config, direct_command(name), output_dir / name, fault)
            outcomes[fault] = result
        passed = all(result["exit_code"] == codes["contract"] and result["answered"]
                     and not result["wall_timeout"] and not result["later_requests"]
                     and result["error"] is None for result in outcomes.values())
        return _verdict(passed, "Each invalid answer must be rejected with the contract exit code",
                        [f"c4-{fault}" for fault in outcomes],
                        exits={key: value["exit_code"] for key, value in outcomes.items()})

    def c5():
        records = _read_rows(output_dir / "c3-original" / "decision_tape.jsonl")
        records[0]["hash"] = "0" * 64
        tampered = output_dir / "tampered-tape.jsonl"
        tampered.write_bytes(b"".join(canonical_json(record) + b"\n" for record in records))
        replay = case("c5-divergence", decider="tape", tape_path=tampered)
        return _verdict(replay["exit_code"] == codes["divergence"] and not replay["wall_timeout"],
                        "A changed request hash must produce the divergence exit code",
                        ["tampered-tape.jsonl", "c5-divergence"], exit_code=replay["exit_code"])

    def c6():
        arguments = config.checks.get("oracle_args")
        if not arguments or config.replay_command is None:
            return {"status": "unmeasured", "detail": "Oracle fixture arguments and input replay are required",
                    "evidence": []}
        duration = config.checks.get("oracle_seconds", config.duration)
        original = case("c6-oracle", duration=duration, extra_args=tuple(arguments))
        replay = case("c6-replay", replay_inputs=output_dir / "c6-oracle" / "inputs.json")
        first = _telemetry(output_dir / "c6-oracle", {"decision"})
        second = _telemetry(output_dir / "c6-replay", {"decision"})
        endings = [[row for row in rows if row["type"] == "run_end"] for rows in (first, second)]
        expected = config.checks.get("oracle_kind", "stuck")
        matching = (all(len(rows) == 1 for rows in endings)
                    and endings[0][0].get("outcome") == "oracle"
                    and endings[0][0].get("oracle") == expected
                    and endings[0][0].get("oracle") == endings[1][0].get("oracle"))
        passed = (_completed(original, codes["oracle"]) and _completed(replay, codes["oracle"])
                  and matching and first == second)
        return _verdict(passed, "The forced stuck oracle must stop and reproduce from recorded inputs",
                        ["c6-oracle", "c6-replay"], identical=first == second,
                        expected_oracle=expected,
                        observed_oracles=[rows[0].get("oracle") if rows else None for rows in endings])

    def c7():
        template = config.checks.get("visibility_command")
        if not template:
            return {"status": "unmeasured", "detail": "A game-owned visibility fixture is not configured",
                    "evidence": []}
        result = _run_process(config, direct_command("c7-visibility", template=template),
                              output_dir / "c7-visibility")
        text = (output_dir / "c7-visibility" / "stdout.log").read_text(encoding="utf-8")
        fixture = json.loads(text)
        canonical_json(fixture)  # Refuse nonfinite fixture evidence before making a verdict.
        passed = (result["exit_code"] == 0 and not result["wall_timeout"]
                  and type(fixture) is dict and fixture.get("passed") is True)
        return _verdict(passed, "The game-owned no-leak fixture must explicitly pass",
                        ["c7-visibility"], fixture=fixture)

    for name, operation in (("C1", c1), ("C2", c2), ("C3", c3), ("C4", c4),
                            ("C5", c5), ("C6", c6), ("C7", c7)):
        perform(name, operation)
    statuses = {value["status"] for value in checks.values()}
    status = "failed" if "failed" in statuses else "unmeasured" if "unmeasured" in statuses else "passed"
    report = {"v": 1, "status": status, "config": str(config.path),
              "seed": seed, "map": map_name, "profile": profile, "checks": checks}
    (output_dir / "conformance.json").write_bytes(canonical_json(report) + b"\n")
    return report
