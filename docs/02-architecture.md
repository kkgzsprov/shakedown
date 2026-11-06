**English** | [한국어](ko/02-architecture.md)

# 2. Architecture

## 2.1 Process model

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
| Triage | Python | Groups findings by signature, confirms reproduction from the tape, asks the planner for a bug draft (chapter 6) |
| Optimizer | Python | Random and grid search inside the tuning key table (chapter 7) |
| Agent skill | Markdown + scripts | Lets a coding agent run all of the above and write an adapter (chapter 5) |
| Example game | Python | A small real-time game with a reference adapter and switchable seeded bugs (chapter 8) |

The Python side uses only the standard library (`json`, `subprocess`, `urllib`, `hashlib`, `concurrent.futures`, `random`, `statistics`). The prototype held to this rule, and it keeps installation to one `pip install`. An optional dependency such as Optuna would come only behind an extra and a separate decision. The minimum version is Python 3.11, the first release with `tomllib` for reading `shakedown.toml`. CI runs the tests, including a fixed request-hash value, on 3.11, 3.12 and 3.13.

## 2.3 Repository layout (planned)

```text
shakedown/
  README.md, README.ko.md
  docs/                 English design docs
  docs/ko/              Korean translations, same file names
  src/shakedown/
    protocol.py         wire format, request hashing, version checks
    bridge.py           one game process ↔ one decider
    deciders/           random.py, mock.py, tape.py, jev.py
    planner/            claude_cli.py, codex_cli.py, mock.py, schemas/
    orchestrate.py      batches → runs → workers
    aggregate.py        metrics.json
    explore.py          QA sampling
    anomalies.py        anomalies.md
    triage.py           findings/
    optimize.py         tuning search
    campaign.py         the planner ↔ optimizer loop with approval gates
    conformance.py      adapter conformance checks (section 3.10)
    cli.py              `shakedown bridge | run | explore | triage | campaign | check-adapter | bench`
  skill/shakedown/      SKILL.md and helper scripts for coding agents
  examples/tinyraid/    reference game and adapter (chapter 8)
  tests/
```

Each file name maps to one stage of the pipeline above. The prototype's tools had the same split and stayed between 26 and 344 lines per file.

## 2.4 Configuration

A project describes its game in `shakedown.toml`. The game's own flag names vary, so Shakedown never hard-codes them. It fills a command template instead.

```toml
[game]
command = ["{exe}", "--headless", "--seed", "{seed}", "--map", "{map}",
           "--duration", "{seconds}", "--telemetry", "{run_dir}/telemetry.jsonl",
           "--record-inputs", "{run_dir}/inputs.json", "--decider", "{decider_mode}",
           "--profile-name", "{profile}", "--tuning", "{tuning_file}"]
replay_command = ["{exe}", "--headless", "--seed", "{seed}", "--map", "{map}",
                  "--replay-inputs", "{inputs_file}", "--telemetry", "{run_dir}/telemetry.jsonl"]
exe = "build/mygame"
maps = ["yard", "warehouse"]
profiles = ["cautious", "aggressive", "looter"]

[decider]
backend = "random"        # random | mock | tape | jev
budget_usd = 0.0

[planner]
backend = "mock"          # mock | claude-cli | codex-cli
budget_usd = 0.0

[tuning]
keys = "tuning_keys.json" # chapter 7
```

`{decider_mode}` expands to `bridge`, `rule`, or `tape:<file>` (section 3.2). Every run writes the fully expanded command to `run.json`, so any run can be repeated by hand.

## 2.5 Run directory

| File | Written by | Contents |
|---|---|---|
| `run.json` | Orchestrator | Expanded command, seed, map, profile, tuning hash, decider, planner instruction hash, Shakedown and adapter versions |
| `telemetry.jsonl` | Adapter | Game events plus `decision` lines with deterministic fields only |
| `decision_tape.jsonl` | Bridge | Per decision: request hash, candidates, order seed, full answer, source, attempts, latency, model ID, applied step, drop reason |
| `inputs.json` | Adapter | Every input event the executor injected, with exact step times |
| `bridge_summary.json` | Bridge | Decision count, fallback rate, cost, latency percentiles, `valid` flag |
| `stdout.log` | Orchestrator | The game's other output |

Section 3.8 defines what the adapter must write. Section 4.4 defines when a run is marked invalid.
