**English** | [한국어](ko/06-oracles-and-triage.md)

# 6. Oracles, exploration, and triage

M3 implements the QA path: oracles and execution failures flag runs, coverage sampling chooses what to try, measured anomaly rates identify groups to investigate, and triage verifies reproductions. Reports keep signals, confirmed reproductions, and missing evidence separate. The LLM planner remains M4 work.

## 6.1 Code oracles

Oracles run in the adapter on every step, so they are deterministic and replay with the run.

| Oracle | Default rule | Action |
|---|---|---|
| Stuck | Under 0.5 m of movement in 10 s during a movement tactic | Stop, exit 4, keep `inputs.json` |
| Teleport | More than a set distance in one step (2 m in the prototype) | Same |
| Numeric | NaN or Inf in position or health, health out of range, negative ammo | Same |
| Out of world | Below terrain or outside the map bounds | Same |
| Enemy out of world | An NPC outside the map bounds | Same |
| No progress | No new room or waypoint in 120 s out of contact | Warning only |
| Crash | Process exits abnormally | The orchestrator reruns from the tape and records whether it reproduces |

Adapters add game-specific oracles, such as a door opened without its key or a purchase without funds. playtest-agent's generic oracle list is a good starting set for that.

Set bounds and exploration ranges per map. In the prototype, the world bound had to be 450 m to include guards in an underground facility 320 m outside the main field. Exploration waypoints clamped to a fixed ±200 m sent the bot to the edges of small maps, where the stuck oracle could fire on the map edge.

## 6.2 Exploration

`shakedown explore` precomputes a seeded plan within `--runs` and optionally stops at `--deadline` wall-clock seconds. A cell is `(map, profile, decider, variant)`. With steering enabled, the next draw uses a least-covered cell; ties are seeded. Each cell exhausts its shuffled configured seed pool before reusing seeds. The same settings and sampler seed produce the same plan independently of execution timing.

`[explore].variants` contains named argument arrays. An adapter may expose night, equipment, or another situation through those arguments; Shakedown does not invent unsupported game flags. `--tuning-noise 1` through `3` samples keys inside the validated key table and records both the values and noisy keys. `--no-steer` disables coverage-based selection.

Exploration executes sequentially so no queued run starts after the deadline. It also caps the active run's timeout by the remaining budget. `exploration.json` retains every planned draw and explicit skipped draws; `batch.json` and metric denominators count only attempted runs. Every attempt records its variant, arguments, seed, tuning, and outcome. Interruption cancels the active process and preserves its attempt without automatically retrying it.

Oracle stops and invalid attempts go to `findings.jsonl`. CLI `run` and `explore` also write the anomaly reports below. See the root README for a bounded command sequence.

## 6.3 Anomalies

`anomalies.json` and `anomalies.md` report measured rates, their opportunity denominators, and Wilson lower bounds. A group is flagged only when it has at least the configured `min_runs` (default 5), its 95% Wilson lower bound exceeds the signal threshold, and that bound exceeds the measured overall rate. `--baseline` supplies a separate clean batch; otherwise the overall rate comes from the same batch. A group cannot raise a signal merely by comparing its rate to itself.

Default signals cover unwarned explosion deaths, extraction-zone timeouts, death-cause shares, early contact, ordinary timeouts, runs with contact but no shots, and observed loot opportunities without loot. Denominators count measured runs, not repeated events from one run. A no-shot signal requires contact and shot instrumentation; no-loot requires an explicit opportunity; an extraction-zone timeout requires an extraction attempt, measured zone dwell, and explicit final occupancy inside the zone. Contradictory counters and event evidence invalidate the observation. Oracle-censored runs and invalid executions do not become ordinary behavioral successes or failures.

Thresholds, minimum count, and grouping can be set under `[anomalies]`. Missing instrumentation or baseline opportunities produce `unmeasured`, not a zero rate. Positive supporting run IDs connect a flagged group to triage, which checks the underlying condition again during reproduction. Signals say where to investigate; they do not identify a cause or establish a bug verdict. The analyzer never reads the planted-fault switches.

## 6.4 Triage and clips

`shakedown triage` validates findings against the submitted inventory, source configuration hash, and actual run evidence before grouping them.

1. **Signature.** Oracle/execution findings use outcome, oracle kind, map, detail with digits removed, and position rounded to 25 m. Anomalies use the signal and supporting map/profile/variant group. No planted-fault label enters the signature.
2. **Reproduction.** The first new signature reruns a bridge decision tape or the original rule arguments, preserving extra arguments, seed, duration, and tuning. Oracle/anomaly confirmation also requires an independent `inputs.json` replay with equal game events. An anomaly must reproduce its actual signal condition, not merely end with the same timeout or death. Replay assertion code 3 is not confirmation. Execution failures use their matching failure class and evidence; missing logs or executable evidence remain unmeasured.
3. **Draft.** M3 writes a deterministic `bug_draft.json` from the evidence, commands, and last events. `suspected_cause` is null. It does not call a planner or invent a cause; LLM drafting is future M4 work.
4. **Clip.** With `--clips`, independent input replay must match the source before frames can become a clip. The default encoder writes an animated GIF from tinyraid's RGB8, noninterlaced, filter-0 PNG output. Unsupported inputs stay unmeasured; replay mismatches or encoder failures are failed clip attempts. A configured argument-array encoder can produce another format without adding a mandatory dependency.

`findings/index.json` and `findings/README.md` keep signature states and source receipts. Repeated calls deduplicate a source path plus evidence hash. New supporting sources update its count without implicitly retrying the first reproduction, including an interrupted attempt. Saved reproduction verdicts are revalidated against retained proof hashes when the catalogue is reopened; changed or missing proof becomes unmeasured without an automatic retry. The catalogue distinguishes `reproduced`, `unreproduced`, and `unmeasured`; clip status remains separate. Triage never changes game files.

The example's `[clips]` settings request a frame every six 60 Hz steps, yielding 10 fps. Clips select the final requested simulation interval (10 seconds by default) and retain replay and frame evidence. Optional `encoder_command` placeholders include `{frames_dir}`, `{output_file}`, `{fps}`, `{seconds}`, `{python}`, and `{project_dir}`; commands execute without a shell.

## 6.5 Metrics

After each batch, M2 writes `metrics.json` grouped by profile × map × tuning hash × decider. Each metric has a per-run mean, bootstrap 95% interval (`ci95`), and its own measured-run count (`n`). The bootstrap seed and sample count are recorded. Missing observability is `mean: null`, `ci95: null`, `n: 0`, not a successful zero. One observation or constant observed values gives a zero-width bootstrap interval. That interval reflects the observed sample and does not prove population certainty, including when every sampled run extracts successfully.

The submitted batch inventory fixes the denominator. Missing folders, malformed telemetry, contract errors, divergence, crashes, timeouts, and invalid bridge summaries stay in `invalid_runs` and do not enter policy comparisons. A structurally valid oracle stop contributes to `oracle_rate`; survival and extraction are censored for that stopped run rather than counted as ordinary failure or success.

Game metrics use available telemetry: survival/extraction, session length, first contact, deaths per contact, emitted shots, healing use, and loot. First-observer and unseen-shooter metrics stay unmeasured when their evidence is absent. A shot count does not assume an arbitrary game consumes one ammunition unit per shot. Tinyraid explicitly emits supported zero counters in `run_end`, distinguishing a measured zero from unavailable instrumentation.

Framework metrics use decision receipts, tapes, and bridge summaries: decisions, dropped-decision rate, longest repeated tactic, fallback rate, cost, and latency. Cell latency p50/p95 values are means of each run's percentile, not percentiles pooled across all calls.

Paired comparisons match identical seed IDs within the same profile and map. They change either decider or tuning, never both at once, and report right-minus-left differences with paired bootstrap intervals. Seeds duplicated in a cell are listed as ambiguous and excluded from paired inference. These comparisons do not establish human fairness or M3 bug-detection recall.
