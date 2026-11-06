"""Request-local uniformly random decisions."""

import random as _random

from shakedown.protocol import request_hash

from . import Decision


class RandomDecider:
    def __init__(self, seed: int = 0):
        self.seed = seed

    def decide(self, request: dict, instructions: str) -> Decision:
        names = sorted(request["candidates"])
        choice = _random.Random(f"{self.seed}:{request_hash(request)}").choice(names)
        return Decision(choice=choice, probabilities={}, source="random")

    def close(self) -> None:
        pass
