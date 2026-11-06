**English** | [한국어](README.ko.md)

# Shakedown

AI playtester for games: an LLM plans test campaigns, a fast decision model picks tactics, and a game-side executor plays through the real input path. Every decision is taped, so every bug replays deterministically.

> Status: design. No code runs yet. See [the design documents](docs/README.md) and [the roadmap](docs/09-roadmap.md).

## How it works

Shakedown runs bot players on three layers, each working at a different speed:

- **Planner (System 2).** An LLM such as Claude Code or Codex writes the test campaign between batches of runs. It picks player profiles, states hypotheses, and sets the tuning ranges to explore. It is never called during play.
- **Decider (System 1).** A fast decision model chooses one tactic from a list of legal options that your game builds. It does not compute coordinates or aim. A random baseline, a game rule, and a tape replayer work with no network.
- **Executor.** Code in your game turns the chosen tactic into keyboard and mouse input every frame, through the same input path a human player uses. It is part of a per-game adapter that the bundled agent skill helps you write.

Game time pauses while the decider thinks, so model latency never changes the outcome. Each answer is written to a tape, so any run can be replayed exactly without calling the model again.

Code-level oracles check for stuck players, teleports, out-of-range stats, and crashes. Every finding comes with its seed, a replay file, and optionally a short video. Between batches, an optimizer proposes balance changes. They are validated on held-out seeds, written as a patch, and never applied without a human.

## Requirements (planned)

- Python 3 standard library for the Shakedown side.
- A game that can run headless on a fixed simulation step from a seed, and pause its own clock. Chapter 3 of the design lists the full contract.

## License

Not chosen yet (see [open questions](docs/09-roadmap.md#94-open-questions)).
