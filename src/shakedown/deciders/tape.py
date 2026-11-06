"""Sequential replay of recorded decisions."""

import json
from pathlib import Path

from shakedown.protocol import ProtocolError, request_hash

from . import Decision


class TapeDivergence(Exception):
    """The game request or chosen action does not match the recording."""


def read_tape(path: str | Path) -> list[dict]:
    records = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, start=1):
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ProtocolError(f"invalid tape JSON at line {number}") from exc
            if not isinstance(record, dict):
                raise ProtocolError(f"invalid tape record at line {number}")
            records.append(record)
    return records


class TapeDecider:
    def __init__(self, path: str | Path):
        self.records = read_tape(path)
        self.cursor = 0

    def decide(self, request: dict, instructions: str) -> Decision:
        actual_hash = request_hash(request)
        seq = request["seq"]
        if self.cursor >= len(self.records):
            raise TapeDivergence(
                f"seq {seq}: tape hash <exhausted>, request hash {actual_hash}"
            )
        record = self.records[self.cursor]
        self.cursor += 1
        expected_hash = record.get("hash")
        if expected_hash != actual_hash:
            raise TapeDivergence(
                f"seq {seq}: tape hash {expected_hash}, request hash {actual_hash}"
            )
        choice = record.get("choice")
        if type(choice) is not str or choice not in request["candidates"]:
            raise TapeDivergence(
                f"seq {seq}: tape hash {expected_hash}, request hash {actual_hash}; "
                f"choice {choice!r} not in candidates"
            )
        return Decision(
            choice=choice, probabilities=record.get("probabilities") or {},
            source="tape",
        )

    def close(self) -> None:
        pass
