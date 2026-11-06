**English** | [한국어](ko/02-architecture.md)

# 2. Architecture

## 2.1 Process model

The diagram is the full design. M0–M3 supply the game/adapter, offline deciders, bridge, replay, isolated batches, aggregation, conformance checks, and the QA path. M4 supplies between-run planner drafts and approval/budget accounting. M5 supplies numeric search, frozen holdout selection, and proposal patches. M6 supplies the agent skill and a second thin-executor example; M7 supplies the optional Jev HTTP backend. Live provider, broader unseen-game, and human-play qualification remain separate gaps. Automatic default application remains unimplemented.

```text
 human: approves goals, campaigns, and any default change
   │
 [planner]  campaign.json ── experiments, hypotheses, profile instructions, tuning ranges
   │                              ▲
   ▼                              │ metrics.json, anomalies.md, findings/
 shakedown orchestrator ──────────┤
   │  (tuning × seed × profile × map → N worker processes)
   ▼
 game process + adapter          ← no network code
   │   every step: observe → reflexes → executor → input events → simulation step
   │   decision point: pause clock, write "@@shakedown {request}" to stdout,
   │                   read one answer line from stdin
   ▼
 shakedown bridge ── decider: random | mock | tape | jev | (game rule, in-process)
   │
   ▼
 runs/<run>/ run.json, telemetry.jsonl, decision_tape.jsonl, inputs.json, stdout.log
```

The game process talks only through stdin and stdout. API keys, retries, budgets, and caching all live in the Python bridge, so the game needs no HTTP client and no new dependency.

One bridge process serves one game process. The orchestrator starts N pairs in parallel and gives each run its own working directory, because games that save profiles or settings to disk will otherwise race each other (the prototype hit this with a shared `profile.json`).

## 2.2 Components

| Component | Language | Responsibility |
|---|---|---|
| Adapter | Game's language | Observation, candidates, decision points, executor, reflexes, oracles, pause, telemetry, input recording (chapter 3) |
| Bridge | Python | Reads requests, calls the decider, writes the tape, enforces budget, marks invalid runs (chapter 4) |
| Deciders | Python | `random`, `mock`, `tape`, `jev`; later local open models (chapter 4) |
| Planner | Python + LLM CLI | `campaign`, `revise`, `bug`, `report`, `targets` steps with JSON Schema-checked output (chapter 5) |
| Orchestrator | Python | Expands a batch into runs, runs workers, collects results |
| Aggregation | Python | Per-cell metrics with bootstrap 95 % intervals and same-seed paired comparison |
| Explorer | Python | QA sampling that favors under-visited situation cells (chapter 6) |
| Anomalies | Python | Flags rates that cross a threshold by their Wilson lower bound (chapter 6) |
| Triage | Python | Groups findings, verifies tape/argument and independent input replay, and writes evidence-only drafts (chapter 6) |
| Optimizer | Python | Random and grid search inside the tuning key table (chapter 7) |
| Agent skill | Markdown + scripts | Lets a coding agent run all of the above and write an adapter (chapter 5) |
| Example game | Python | A small real-time game with a reference adapter and switchable seeded bugs (chapter 8) |

The Python side uses only the standard library (`json`, `subprocess`, `urllib`, `hashlib`, `concurrent.futures`, `random`, `statistics`). The prototype held to this rule, and it keeps installation to one `pip install`. An optional dependency such as Optuna would come only behind an extra and a separate decision. The minimum version is Python 3.11, the first release with `tomllib` for reading `shakedown.toml`. CI runs the tests, including a fixed request-hash value, on 3.11, 3.12 and 3.13.

## 2.3 Repository layout (implemented and planned)

```text
shakedown/
  README.md, README.ko.md
  docs/                 English design docs
  docs/ko/              Korean translations, same file names
  src/shakedown/
    protocol.py         wire format, request hashing, version checks
    config.py           validated TOML and argument templates
    processes.py        bounded child processes and cancellation
    windows_job.py      Windows descendant containment
    bridge.py           one game process ↔ one decider
    deciders/           random.py, mock.py, tape.py, jev.py
    planner/            CLI backends, schemas, approvals, budgets, baseline campaigns
    orchestrate.py      batches → runs → workers
    aggregate.py        metrics.json
    explore.py                    QA sampling
    anomalies.py                anomalies.md
    triage.py                      findings/
    clips.py            verified replay frames and encoding
    bench.py            controlled tinyraid qualification
    optimize.py         bounded random/grid search and measured target distances
    balance_gate.py     holdout improvement, oracle, sample, and policy checks
    campaign.py         approved search/revision loop and frozen holdout evaluation
    proposals.py        reviewable tuning-default patches; never applies them
    conformance.py      adapter conformance checks (section 3.10)
    cli.py              `shakedown bridge | run | check-adapter | explore | anomalies | triage | bench | plan | approve | campaign`
  skill/shakedown/      agent skill, adapter guides, transport skeletons, wrapper
  examples/tinyraid/    reference game and adapter (chapter 8)
  examples/switchyard/  second game with a discrete-button thin executor
  tests/
```

Each file name maps to one stage of the pipeline above. The prototype's tools had the same split and stayed between 26 and 344 lines per file.

## 2.4 Configuration

A project describes its game in `shakedown.toml`. M2 loads it with `tomllib`, validates types and tuning bounds, then expands argument arrays without a shell. The following is the runnable `examples/tinyraid/shakedown.toml` configuration:

```toml
[game]
cwd = "../.."
command = ["{python}", "-m", "examples.tinyraid", "--headless", "--seed", "{seed}", "--map", "{map}", "--duration", "{seconds}", "--telemetry", "{run_dir}/telemetry.jsonl", "--record-inputs", "{run_dir}/inputs.json", "--decider", "{decider_mode}", "--profile-name", "{profile}", "--tuning", "{tuning_file}"]
replay_command = ["{python}", "-m", "examples.tinyraid", "--replay-inputs", "{inputs_file}", "--telemetry", "{run_dir}/telemetry.jsonl"]
maps = ["yard", "warehouse"]
profiles = ["cautious", "aggressive", "looter"]

[batch]
seeds = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
deciders = ["rule"]
seconds = 12
workers = 2
wall_timeout_s = 30
bootstrap_samples = 1000
bootstrap_seed = 0

[decider]
backend = "rule"
budget_usd = 0.0

[tuning]
keys = "tuning_keys.json"
candidates = [{}]

[checks]
oracle_args = ["--scenario", "stuck"]
oracle_seconds = 14
visibility_command = ["{python}", "-m", "examples.tinyraid.checks"]

[anomalies]
min_runs = 5

[clips]
replay_args = ["--frame-dir", "{frame_dir}", "--frame-every", "6"]
fps = 10
simulation_hz = 60
```

`{decider_mode}` expands to `bridge`, `rule`, or `tape:<file>` (section 3.2). Every run writes the fully expanded command to `run.json`, so any run can be repeated by hand.

Configuration paths resolve from the TOML file. `game.cwd` selects the project root, not the child's working directory. Each child runs in its private `runs/<id>/work/`; use `{project_dir}` for assets that stay in the project. `game.fixtures` optionally lists files relative to that root; the runner copies a fresh copy into each working directory. Absolute fixture paths and paths escaping the root are rejected.

`{python}` is the current interpreter; `{exe}` uses the optional `game.exe`. Other supported fields are `{project_dir}`, `{seed}`, `{map}`, `{profile}`, `{seconds}`, `{run_dir}`, `{decider_mode}`, `{tuning_file}`, and `{inputs_file}`. Attributes, indexing, format specifiers, and conversions are rejected. A literal argument containing shell punctuation stays one argument. Instruction and tuning-key file paths resolve relative to the config file. Nonempty tuning candidates require a key table; unknown keys, wrong types, and values outside its bounds are rejected.

M2 batch backends are `rule`, `mock`, and `random`. `tape:<file>` remains a replay mode, not a batch policy. Planner settings can describe later work but do not run a planner in M2. `game.exit_codes` can remap `ok`, `contract`, `oracle`, and `divergence` to distinct nonnegative codes.

## 2.5 Run directory

| File | Written by | Contents |
|---|---|---|
| `run.json` | Orchestrator | Expanded command, seed, map, profile, tuning hash, decider, planner instruction hash, Shakedown and adapter versions |
| `telemetry.jsonl` | Adapter | Game events and deterministic `decision` receipts: `seq`, choice, reason, status (`selected`, `applied`, `dropped`), `applied_step` or `drop_reason` where applicable |
| `decision_tape.jsonl` | Bridge | Per decision: request hash, candidates, order seed, choice, probabilities, source, attempts, latency, model ID; records the selection before the game acts |
| `inputs.json` | Adapter | Every input event the executor injected, with exact step times |
| `bridge_summary.json` | Bridge | Decision count, fallback rate, cost, latency percentiles, `valid` flag |
| `stdout.log` | Orchestrator | The game's other output |

Section 3.8 defines what the adapter must write. Section 4.4 defines when a run is marked invalid.

Join a tape row to the adapter's application/drop receipt by `seq`. The bridge cannot know a delayed choice's outcome when it writes the selection. This split keeps wire protocol v1 unchanged. The bridge or direct runner writes `stdout.log`; M2 writes `run.json` for every attempt, including launch failures. It records the expanded argument array, private working directory, config path/hash, seed, map, profile, tuning hash, instruction hash, exit mapping, and versions. An unavailable adapter version is null, not inferred.

## 2.6 Batch lifecycle

`run` expands seeds × maps × profiles × offline backends × tuning candidates into stable run IDs and executes at most the configured worker count. Each attempt has a finite wall-clock timeout and is never retried automatically. On POSIX, children use process groups; on Windows, the runner creates the child suspended, assigns a kill-on-close job, and resumes it so descendants remain contained even if the game parent exits.

The output directory must be new. `batch.json` retains the submitted `planned_runs` inventory and completed attempt records. Missing run folders remain invalid entries in the original denominator. `findings.jsonl` includes oracle findings and invalid attempts. `metrics.json` records the independent evidence validation and cell summaries. Ctrl+C cancels queued and running work, records remaining attempts, and marks the batch `interrupted`; it does not silently rerun them.

## 2.7 QA artifacts and boundaries

M3 adds `exploration.json` (the full sampling plan and skipped draws), `anomalies.json`/`anomalies.md` (measured investigation signals), and a persistent triage catalogue under its own output directory. Signature folders hold drafts, original source receipts, policy/tape reproductions, independent input replay, and optional `clip.json` plus an encoded clip. These stages reuse the isolated M2 runner and do not edit game defaults.

`[explore].variants` adds game-owned argument arrays to coverage cells. `[anomalies]` configures minimum runs, thresholds, and grouping. `[clips]` configures frame-replay arguments and an optional encoder. Their settings remain separate from the game's ordinary command template; neither a variant name nor a planted-fault label is a detection verdict. The controlled benchmark records a frozen seed split and normalized plan hash alongside its results.
