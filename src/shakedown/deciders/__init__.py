"""Local decider interface and factory."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Decision:
    choice: str
    probabilities: dict[str, float]
    source: str
    attempts: int = 0
    latency_ms: float = 0.0
    model: str | None = None


class Decider(Protocol):
    def decide(self, request: dict, instructions: str) -> Decision: ...

    def close(self) -> None: ...


class DeciderError(ValueError):
    """A hosted provider failed permanently; stop and mark the run invalid."""


def make_decider(name: str, *, seed: int = 0, tape_path: str | None = None,
                 hosted_runtime=None, run_id: str | None = None) -> Decider:
    if name == "random":
        from .random import RandomDecider
        return RandomDecider(seed)
    if name == "mock":
        from .mock import MockDecider
        return MockDecider()
    if name == "tape":
        if tape_path is None:
            raise ValueError("tape requires tape_path")
        from .tape import TapeDecider
        return TapeDecider(tape_path)
    if name == "jev":
        if hosted_runtime is None or run_id is None:
            raise ValueError("jev requires a configured shared runtime and run identity")
        return hosted_runtime.new_decider(seed=seed, run_id=run_id)
    raise ValueError(f"unknown decider: {name}")
