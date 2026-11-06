**English** | [한국어](README.ko.md)

# Shakedown

An AI playtesting pipeline for games, with an offline test path. A game-side executor plays through the ordinary input path, and Shakedown records decisions and inputs so supported deterministic failures can be reproduced. The M4 planner drafts structured artifacts between runs and executes an approved baseline campaign.

> Status: early. M0-M3 provide protocol and offline deciders, tinyraid and replay, isolated batches, adapter checks, and the QA path. M4 adds planner drafts, budget accounting, approval receipts, and one baseline campaign round. Live CLI qualification remains pending; M5 and later remain design. See [the design documents](docs/README.md) and [the roadmap](docs/09-roadmap.md).

## How it works

The full design has three layers, each working at a different speed. The M4 planner runs between batches; gameplay still uses offline deciders:

- **Planner (System 2).** An LLM such as Claude Code or Codex writes the test campaign between batches of runs. It picks player profiles, states hypotheses, and sets the tuning ranges to explore. It is never called during play.
- **Decider (System 1).** A fast decision model chooses one tactic from a list of legal options that your game builds. It does not compute coordinates or aim. A random baseline, a game rule, and a tape replayer work with no network.
- **Executor.** Code in your game turns the chosen tactic into keyboard and mouse input every frame, through the same input path a human player uses. It is part of a per-game adapter that the bundled agent skill helps you write.

Game time pauses while the decider thinks, so model latency never changes the outcome. Each answer is written to a tape, so any run can be replayed exactly without calling the model again.

Code-level oracles check stuck players, teleports, and out-of-range stats; the runner records crashes and other invalid attempts. Triage distinguishes reproduced findings from missing or failed evidence and can make a replay-backed clip. The optimizer and balance-proposal loop remain planned.

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

The bridge writes `decision_tape.jsonl`, `bridge_summary.json`, and `stdout.log`; the game writes the requested telemetry and input recording. [Chapter 8](docs/08-example-game.md) documents the fixed M1 seed matrix, bug fixtures, and optional PNG frames. The commands below cover batch execution, conformance, QA, and the M4 planner.

## Batch runs and adapter checks

After the editable install above, this short batch runs one seed across two maps and three profiles. Adapter checks keep their own evidence:

```bash
python -m shakedown run --config examples/tinyraid/shakedown.toml --output runs/tinyraid-batch --seeds 1 --seconds 1 --workers 2
python -m shakedown check-adapter --config examples/tinyraid/shakedown.toml --output runs/tinyraid-checks
```

Use a new output directory for each invocation; existing evidence is never overwritten. Omit `--seeds` and `--seconds` to use the example's configured 60 runs (10 seeds × two maps × three profiles, 12 seconds each). `--seconds 45` raises the simulation limit, and `--deciders rule,mock,random` adds offline policy comparisons.

A batch writes `batch.json`, `metrics.json`, `findings.jsonl`, and a directory per run. Invalid executions remain in the submitted count and are excluded from comparisons. Adapter checks write `conformance.json` with C1–C7 verdicts and evidence paths. Missing game-specific fixtures are `unmeasured`, never passed.

The `run` command exits 0 with no findings, 1 when findings exist, 2 for configuration errors, and 130 after an interrupted batch has recorded its remaining attempts. `check-adapter` exits 0 only when every check passes, 1 for failed or unmeasured checks, and 2 for configuration errors. These CLI codes differ from the game's oracle exit code 4.

## Exploration, triage, and clips

A bounded exploration uses the configured maps, profiles, and offline deciders. These commands keep sampled plans, measured signals, and reproduction evidence separate:

```bash
python -m shakedown explore --config examples/tinyraid/shakedown.toml --output runs/tinyraid-explore --runs 6 --seed 17
python -m shakedown anomalies --config examples/tinyraid/shakedown.toml --batch runs/tinyraid-explore
python -m shakedown triage --config examples/tinyraid/shakedown.toml --batch runs/tinyraid-explore --output runs/tinyraid-triage --clips
```

Exploration favors the least-covered map/profile/decider/variant cell and exhausts each cell's shuffled seed pool before reusing seeds. It is sequential so `--deadline <seconds>` can stop new work and bound the active run. `exploration.json` retains the complete plan and skipped draws; the batch denominator contains attempted runs. `--tuning-noise 1` through `3` samples bounded key-table values; `--no-steer` disables coverage steering.

`anomalies.json` and `anomalies.md` identify measured groups to investigate. They require a minimum sample count, a Wilson lower bound above the configured threshold, and a rate above the measured overall or `--baseline` clean batch rate. Missing opportunities remain unmeasured. A flag is not a confirmed bug.

Triage keeps `findings/index.json`, a summary, source hashes, deterministic `bug_draft.json` files, and reproduction attempts. Oracle and anomaly confirmation requires both policy/tape reproduction and independent input replay. Existing signatures are deduplicated without silently retrying them. Drafts do not call an LLM or invent a cause. With `--clips`, a matching input replay can produce an animated GIF; an optional external encoder supports other formats.

QA CLI exit codes describe the stage: `explore` returns 1 for execution/oracle findings or anomaly flags and 130 for interruption; `anomalies` returns 1 for flags; `triage` returns 1 when any signature is unreproduced or unmeasured. `bench` returns 0 only for a passing qualification gate, 1 for failed or unqualified coverage, and 130 for interruption. Configuration errors return 2.

The controlled tinyraid benchmark uses disjoint search and holdout seeds and applies the same input-workflow probes to clean and single-fault games. See [chapter 8](docs/08-example-game.md#84-benchmark) for the full command, qualification rules, and the boundary between test-bed coverage and autonomous discovery in an unfamiliar game.

## Planner drafts and approved campaigns

M4 adds `plan`, `approve`, and `campaign`. Planning supports `targets`, `campaign`, `revise`, `bug`, and `report`; use `--backend mock` for offline drafts or explicitly configure a CLI backend. Input files are passed with repeated `--input NAME=PATH` arguments. Each draft is schema-checked and semantically validated before it can be used. [Chapter 5](docs/05-planner-and-skill.md) describes the command contract, budgets, and backend restrictions.

```text
python -m shakedown plan targets --config CONFIG --input design_notes=DESIGN_FILE --output DRAFT_DIR --backend mock
python -m shakedown approve targets --config CONFIG --artifact TARGETS_JSON --receipt TARGETS_RECEIPT --approved
python -m shakedown plan campaign --config CONFIG --input design_notes=DESIGN_FILE --targets TARGETS_JSON --targets-approval TARGETS_RECEIPT --output CAMPAIGN_DIR --backend mock
python -m shakedown approve campaign --config CONFIG --artifact CAMPAIGN_JSON --receipt CAMPAIGN_RECEIPT --targets TARGETS_JSON --targets-approval TARGETS_RECEIPT --approved
python -m shakedown campaign --config CONFIG --campaign CAMPAIGN_JSON --approval CAMPAIGN_RECEIPT --targets TARGETS_JSON --targets-approval TARGETS_RECEIPT --output RUN_DIR --approved
```

The uppercase values are paths supplied by the operator. The first plan writes `DRAFT_DIR/targets.json`; the second writes `CAMPAIGN_DIR/campaign.json`. Review each artifact before creating its approval receipt. Receipts bind approval to the artifact and configuration contents; they are unsigned operator assertions, not authenticated signatures. Changing approved content requires a new receipt.

An M4 campaign executes one baseline round. Numeric optimization, iterative search, holdout qualification of a proposed balance change, and applying new defaults remain M5 work. Planner calls reserve a configured amount in a ledger before execution; missing cost data consumes the entire reservation. These reservations bound Shakedown's admitted calls, not the provider's final bill. Codex requires explicit `codex_mode = "restricted"` opt-in and does not offer a verified tool-free guarantee. Manual calls to the installed CLI backends require a separately authorized budget; this milestone's offline validation does not establish live-provider behavior.

## License

Not chosen yet (see [open questions](docs/09-roadmap.md#94-open-questions)).
