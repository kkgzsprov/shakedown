**English** | [한국어](README.ko.md)

# Shakedown

AI playtester for games: an LLM plans test campaigns, a fast decision model picks tactics, and a game-side executor plays through the real input path. Every decision is taped, so every bug replays deterministically.

> Status: early. M0–M2 are implemented: the protocol and offline deciders, the tinyraid reference game and replay, and isolated batches with metrics and adapter checks. M3 and later remain design. See [the design documents](docs/README.md) and [the roadmap](docs/09-roadmap.md).

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

The bridge writes `decision_tape.jsonl`, `bridge_summary.json`, and `stdout.log`; the game writes the requested telemetry and input recording. [Chapter 8](docs/08-example-game.md) documents the fixed M1 seed matrix, bug fixtures, and optional PNG frames. M2 adds the batch and conformance commands below; `bench`, exploration, triage, and the planner remain planned.

## Batch runs and adapter checks

After the editable install above, this short batch runs one seed across two maps and three profiles. Adapter checks keep their own evidence:

```bash
python -m shakedown run --config examples/tinyraid/shakedown.toml --output runs/tinyraid-batch --seeds 1 --seconds 1 --workers 2
python -m shakedown check-adapter --config examples/tinyraid/shakedown.toml --output runs/tinyraid-checks
```

Use a new output directory for each invocation; existing evidence is never overwritten. Omit `--seeds` and `--seconds` to use the example's configured 60 runs (10 seeds × two maps × three profiles, 12 seconds each). `--seconds 45` raises the simulation limit, and `--deciders rule,mock,random` adds offline policy comparisons.

A batch writes `batch.json`, `metrics.json`, `findings.jsonl`, and a directory per run. Invalid executions remain in the submitted count and are excluded from comparisons. Adapter checks write `conformance.json` with C1–C7 verdicts and evidence paths. Missing game-specific fixtures are `unmeasured`, never passed.

The `run` command exits 0 with no findings, 1 when findings exist, 2 for configuration errors, and 130 after an interrupted batch has recorded its remaining attempts. `check-adapter` exits 0 only when every check passes, 1 for failed or unmeasured checks, and 2 for configuration errors. These CLI codes differ from the game's oracle exit code 4.

## License

Not chosen yet (see [open questions](docs/09-roadmap.md#94-open-questions)).
