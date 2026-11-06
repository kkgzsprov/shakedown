"""Connect one game process to one decider and persist its evidence."""

from collections import Counter
from dataclasses import dataclass, field, replace
import json
import math
from pathlib import Path
import subprocess
import threading
import time

from .deciders import Decider
from .deciders.tape import TapeDivergence
from .processes import ProcessControl, close_process, start_process, terminate_process
from .protocol import (
    EXIT_CONTRACT, EXIT_DIVERGENCE, SOURCES, ProtocolError,
    encode_answer, parse_game_line, request_hash,
)


@dataclass
class _Run:
    game: dict | None = None
    end: dict | None = None
    last_seq: int | None = None
    latencies: list[float] = field(default_factory=list)
    sources: Counter = field(default_factory=Counter)
    contract_error: str | None = None
    divergence: str | None = None

    def check_order(self, message: dict) -> None:
        if message["type"] == "hello":
            if self.game is not None or self.last_seq is not None:
                raise ProtocolError("hello must occur once before decide")
        elif message["type"] == "decide":
            if self.last_seq is not None and message["seq"] <= self.last_seq:
                raise ProtocolError("seq must strictly increase")
            self.last_seq = message["seq"]


def _nearest_rank(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    return sorted(values)[math.ceil(len(values) * percentile / 100) - 1]


def _summary(run: _Run, name: str, exit_code: int | None, timed_out: bool) -> dict:
    decisions = len(run.latencies)
    fallback_rate = run.sources["fallback"] / decisions if decisions else 0.0
    reasons = []
    if fallback_rate > 0.10:
        reasons.append("fallback_rate_over_10pct")
    if run.contract_error is not None:
        reasons.append("contract_error")
    if run.divergence is not None:
        reasons.append("divergence")
    if timed_out:
        reasons.append("wall_timeout")
    if exit_code == 0 and run.end is None:
        reasons.append("no_end_message")
    return {
        "decider": name,
        "decisions": decisions,
        "sources": dict(run.sources),
        "fallback_rate": fallback_rate,
        "cost_usd": 0.0,
        "latency_ms_p50": _nearest_rank(run.latencies, 50),
        "latency_ms_p95": _nearest_rank(run.latencies, 95),
        "game": run.game,
        "end": run.end,
        "exit_code": exit_code,
        "contract_error": run.contract_error,
        "divergence": run.divergence,
        "wall_timeout": timed_out,
        "invalid_reasons": reasons,
        "valid": not reasons,
    }


def _record_decision(request: dict, digest: str, decider: Decider,
                     instructions: str, run: _Run, tape) -> str:
    started = time.perf_counter()
    decision = decider.decide(request, instructions)
    decision = replace(decision, latency_ms=(time.perf_counter() - started) * 1000)
    if not isinstance(decision.choice, str) or decision.choice not in request["candidates"]:
        raise RuntimeError("decider choice is not in candidates")
    if not isinstance(decision.source, str) or decision.source not in SOURCES:
        raise RuntimeError("decider source is not supported")
    answer = encode_answer(request["seq"], decision.choice,
                           decision.source, decision.probabilities)
    record = {
        "seq": request["seq"], "step": request["step"], "hash": digest,
        "order_seed": request["order_seed"], "candidates": request["candidates"],
        "choice": decision.choice, "probabilities": decision.probabilities,
        "source": decision.source, "attempts": decision.attempts,
        "latency_ms": decision.latency_ms, "model": decision.model,
    }
    tape.write(json.dumps(record, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False) + "\n")
    tape.flush()  # The evidence must be durable to readers before the game acts.
    run.latencies.append(decision.latency_ms)
    run.sources[decision.source] += 1
    return answer


def _read_game(process, log, tape, decider: Decider, instructions: str,
               run: _Run) -> int | None:
    for raw in iter(process.stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace")
        try:
            message = parse_game_line(line)
            if message is not None:
                run.check_order(message)
                digest = request_hash(message) if message["type"] == "decide" else None
        except ProtocolError as error:
            run.contract_error = str(error)[:300]
            terminate_process(process)
            return EXIT_CONTRACT
        if message is None:
            log.write(line)
            log.flush()
        elif message["type"] == "hello":
            run.game = {key: message[key] for key in
                        ("game", "adapter_version", "seed", "profile")}
        elif message["type"] == "end":
            run.end = {key: message[key] for key in ("outcome", "step", "oracle")
                       if key in message}
        else:
            try:
                answer = _record_decision(message, digest, decider, instructions, run, tape)
            except TapeDivergence as error:
                run.divergence = str(error)[:300]
                terminate_process(process)
                return EXIT_DIVERGENCE
            try:
                process.stdin.write(answer.encode("utf-8"))
                process.stdin.flush()
            except OSError:  # BrokenPipeError on POSIX, EINVAL on Windows
                break
    return None


def run_bridge(command: list[str], run_dir: Path, decider: Decider, *,
               instructions: str = "", decider_name: str,
                wall_timeout_s: float | None = None,
                cwd: Path | None = None, env: dict | None = None,
                control: ProcessControl | None = None) -> int:
    """Run a game until it exits, diverges, violates the contract, or times out.

    Unexpected decider failures are raised, not misreported as adapter errors.
    The child is still reaped and the decider closed on these exceptional paths.
    """
    run_dir = Path(run_dir)
    run = _Run()
    process = timer = None
    timed_out = threading.Event()
    exit_code = None
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "stdout.log").open("w", encoding="utf-8", newline="") as log, \
             (run_dir / "decision_tape.jsonl").open("w", encoding="utf-8", newline="\n") as tape:
            process = start_process(command, control=control, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       bufsize=0, cwd=cwd, env=env)

            def kill_on_timeout():
                timed_out.set()
                terminate_process(process)

            if wall_timeout_s is not None:
                timer = threading.Timer(wall_timeout_s, kill_on_timeout)
                timer.daemon = True
                timer.start()
            exit_code = _read_game(process, log, tape, decider, instructions, run)
    except BaseException:
        if process is not None and process.poll() is None:
            terminate_process(process)
        raise
    finally:
        try:
            if process is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                game_code = process.wait()
                if exit_code is None:
                    exit_code = game_code
                process.stdout.close()
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if process is not None:
                close_process(process)
            try:
                decider.close()
            finally:
                if run_dir.is_dir():
                    summary = _summary(run, decider_name, exit_code, timed_out.is_set())
                    (run_dir / "bridge_summary.json").write_text(
                        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return exit_code
