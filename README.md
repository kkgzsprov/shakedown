**English** | [한국어](README.ko.md)

# Shakedown

AI playtester for games: an LLM plans test campaigns, a fast decision model picks tactics, and a game-side executor plays through the real input path. Every decision is taped, so every bug replays deterministically.

> Status: early. M0 (protocol, bridge, offline deciders) and M1 (the headless tinyraid reference game, adapter, and replay) are implemented. M2 and later remain design. See [the design documents](docs/README.md) and [the roadmap](docs/09-roadmap.md).

## How it works

Shakedown runs bot players on three layers, each working at a different speed:

- **Planner (System 2).** An LLM such as Claude Code or Codex writes the test campaign between batches of runs. It picks player profiles, states hypotheses, and sets the tuning ranges to explore. It is never called during play.
- **Decider (System 1).** A fast decision model chooses one tactic from a list of legal options that your game builds. It does not compute coordinates or aim. A random baseline, a game rule, and a tape replayer work with no network.
- **Executor.** Code in your game turns the chosen tactic into keyboard and mouse input every frame, through the same input path a human player uses. It is part of a per-game adapter that the bundled agent skill helps you write.

Game time pauses while the decider thinks, so model latency never changes the outcome. Each answer is written to a tape, so any run can be replayed exactly without calling the model again.

Code-level oracles check for stuck players, teleports, out-of-range stats, and crashes. Every finding comes with its seed, a replay file, and optionally a short video. Between batches, an optimizer proposes balance changes. They are validated on held-out seeds, written as a patch, and never applied without a human.

## Requirements

- Python 3.11 or later, standard library only, for the Shakedown side.
- A game that can run headless on a fixed simulation step from a seed, and pause its own clock. Chapter 3 of the design lists the full contract.

## Try it

The tests need no network and no install:

```bash
python -m unittest discover -s tests -v
```

From the repository root, run tinyraid with its local rule and replay the recorded inputs without a decider:

```bash
python -m examples.tinyraid --seed 7 --duration 45 --profile-name looter --decider rule --telemetry runs/tinyraid-rule/telemetry.jsonl --record-inputs runs/tinyraid-rule/inputs.json
python -m examples.tinyraid --replay-inputs runs/tinyraid-rule/inputs.json --telemetry runs/tinyraid-input-replay/telemetry.jsonl
```

Install the package to use the bridge, record a `mock` run, then replay the bridge's decision tape inside the adapter:

```bash
python -m pip install --no-build-isolation -e .
python -m shakedown bridge --decider mock --run-dir runs/tinyraid-mock -- python -m examples.tinyraid --seed 7 --duration 12 --decider bridge --telemetry runs/tinyraid-mock/telemetry.jsonl --record-inputs runs/tinyraid-mock/inputs.json
python -m examples.tinyraid --seed 7 --duration 12 --decider tape:runs/tinyraid-mock/decision_tape.jsonl --telemetry runs/tinyraid-tape/telemetry.jsonl
```

The install uses the existing build tools; install `setuptools` first if it is absent. `shakedown` is equivalent to `python -m shakedown` when Python's Scripts directory is on `PATH`. The example stays in this source checkout, so run its commands from the repository root.

The bridge writes `decision_tape.jsonl`, `bridge_summary.json`, and `stdout.log`; the game writes the requested telemetry and input recording. [Chapter 8](docs/08-example-game.md) documents the fixed M1 seed matrix, bug fixtures, and optional PNG frames. `check-adapter` and `bench` are planned commands, not part of M1.

## License

Not chosen yet (see [open questions](docs/09-roadmap.md#94-open-questions)).
