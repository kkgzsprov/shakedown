"""Version 1 of the line protocol and its portable request digest.

The digest is SHA-256 of UTF-8 JSON for an object with exactly ``state``,
``candidates``, ``order_seed``, ``profile``, and ``step``. Serialize with
sorted object keys at every depth, no spaces (separators ``,`` and ``:``),
literal Unicode rather than ASCII escapes, and no NaN or Infinity. Candidate
insertion order therefore has no effect. Other request fields are excluded.
Adapters in other languages must reproduce these bytes exactly; prefer
integers or pre-rounded state values because float text must match Python's
shortest round-trip representation.
"""

import hashlib
import json
import math


PROTOCOL_VERSION = 1
PREFIX = "@@shakedown "
MAX_CANDIDATES = 12
EXIT_OK = 0
EXIT_CONTRACT = 2
# Exit code 3 is reserved for the game.
EXIT_ORACLE = 4
EXIT_DIVERGENCE = 5
SOURCES = frozenset({"random", "mock", "tape", "jev", "cache", "fallback"})
HASHED_FIELDS = ("state", "candidates", "order_seed", "profile", "step")


class ProtocolError(ValueError):
    """A game or answer violates the version 1 contract."""


def canonical_json(value) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError(f"invalid JSON value: {exc}") from exc


def request_hash(request: dict) -> str:
    try:
        fields = {key: request[key] for key in HASHED_FIELDS}
    except (KeyError, TypeError) as exc:
        raise ProtocolError(f"missing hashed field: {exc}") from exc
    return hashlib.sha256(canonical_json(fields)).hexdigest()


def _required(message: dict, field: str, expected: type) -> object:
    if field not in message or type(message[field]) is not expected:
        raise ProtocolError(f"invalid or missing {field}")
    return message[field]


def _version(message: dict) -> None:
    if type(message.get("v")) is not int or message["v"] != PROTOCOL_VERSION:
        raise ProtocolError("invalid v")


def _reject_constant(value: str):
    raise ProtocolError(f"invalid JSON constant: {value}")


def _load_object(text: str) -> dict:
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("expected JSON object")
    return value


def parse_game_line(line: str) -> dict | None:
    line = line.rstrip("\r\n")
    if not line.startswith(PREFIX):
        return None
    message = _load_object(line[len(PREFIX):])
    _version(message)
    kind = _required(message, "type", str)
    if kind == "hello":
        for field, expected in (
            ("game", str), ("adapter_version", str),
            ("seed", int), ("profile", str),
        ):
            _required(message, field, expected)
    elif kind == "decide":
        for field, expected in (
            ("seq", int), ("step", int), ("reason", str),
            ("profile", str), ("state", dict),
            ("candidates", dict), ("order_seed", int),
        ):
            _required(message, field, expected)
        if message["step"] < 0:
            raise ProtocolError("invalid step")
        candidates = message["candidates"]
        if not 1 <= len(candidates) <= MAX_CANDIDATES or any(
            type(name) is not str or type(description) is not str
            for name, description in candidates.items()
        ):
            raise ProtocolError("invalid candidates")
        if "run" in message:
            _required(message, "run", str)
        if "t" in message and (
            type(message["t"]) not in (int, float)
            or (type(message["t"]) is float and not math.isfinite(message["t"]))
        ):
            raise ProtocolError("invalid t")
    elif kind == "end":
        outcome = _required(message, "outcome", str)
        if outcome not in {"death", "extract", "timeout", "oracle"}:
            raise ProtocolError("invalid outcome")
        _required(message, "step", int)
        if "oracle" in message:
            _required(message, "oracle", str)
    else:
        raise ProtocolError("invalid type")
    return message


def encode_answer(seq, choice, source, probabilities) -> str:
    if type(source) is not str or source not in SOURCES:
        raise ProtocolError("invalid source")
    answer = {
        "v": PROTOCOL_VERSION, "seq": seq, "choice": choice,
        "source": source, "probabilities": probabilities,
    }
    return canonical_json(answer).decode("utf-8") + "\n"


def parse_answer(line: str, request: dict) -> dict:
    if not line.endswith("\n"):
        raise ProtocolError("answer missing newline")
    answer = _load_object(line[:-1])
    _version(answer)
    if type(answer.get("seq")) is not int or answer["seq"] != request["seq"]:
        raise ProtocolError("invalid seq")
    if type(answer.get("choice")) is not str or answer["choice"] not in request["candidates"]:
        raise ProtocolError("invalid choice")
    return answer
