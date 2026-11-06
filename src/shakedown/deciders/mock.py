"""Deterministic hash-indexed decisions for offline tests."""

from shakedown.protocol import request_hash

from . import Decision


class MockDecider:
    def decide(self, request: dict, instructions: str) -> Decision:
        names = sorted(request["candidates"])
        choice = names[int(request_hash(request), 16) % len(names)]
        return Decision(choice=choice, probabilities={}, source="mock")

    def close(self) -> None:
        pass
