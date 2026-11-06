**English** | [한국어](ko/06-oracles-and-triage.md)

# 6. Oracles, exploration, and triage

The full QA path has three stages: oracles flag a run, exploration decides which runs to try, and triage turns flagged runs into reproduced bugs. M2 runs configured batches and aggregates their evidence. The exploration, anomalies, triage, and clips described below remain planned for M3.

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

`shakedown explore` samples runs until a wall-clock deadline or a budget. Each run draws a map, profile, decider, time of day, and so on, plus optional tuning noise (one to three keys drawn inside the key table) and optional equipment. Every draw goes into `run.json`.

Exploration steers toward under-visited cells. A cell is a combination such as (map, profile, decider, night), and with steering on, the next run takes the least-covered cell. The sampler is seeded, so the same command draws the same specs. In the prototype's overnight run this covered all 147 cells and split day and night runs about evenly (762 night runs of 1,563).

Runs that end in an oracle stop, a crash, a contract error, a tape divergence, or a wall-clock timeout go to `findings.jsonl`.

## 6.3 Anomalies

Some problems never trip an oracle. In the prototype's overnight run, explosions caused 39.5 % of deaths, median time to first contact was 1.3 s, 53.7 % of runs fired no shot, no run looted anything, and one map timed out in 248 of 260 runs. The overnight report mentioned none of this. It surfaced only when the results were aggregated again.

`anomalies.md` lists rates that cross a threshold, judged by the Wilson lower bound of the rate rather than the raw rate, so small groups do not raise false alarms. A flagged group must have a minimum number of runs, and the report shows a group only when its lower bound is above the overall rate. The file says where to look, not what is wrong, and it goes at the top of every report.

Default checks: death by one cause above a share, contact in the first seconds, timeouts, runs with no shots, runs with no loot, and hazards with no warning. Adapters can add more.

## 6.4 Triage

`shakedown triage` turns findings into bugs.

1. **Signature.** Group findings by outcome, oracle kind, map, the oracle detail with digits removed, and the place rounded to 25 m. One wall that traps the bot is one bug, however many runs hit it.
2. **Reproduce.** Rerun the first run of each new signature: from its tape for a bridge run, from its own arguments for a `rule` run. "Reproduced" means the rerun ends with the same outcome and oracle kind.
3. **Draft.** The planner writes `bug_draft.json` from the run's arguments, the oracle line, and the last telemetry events.
4. **Clip.** Optionally replay `inputs.json` with frame recording on and cut the last seconds to video. The prototype used the game's frame dump and Blender to encode MP4. Shakedown will call a configurable encoder command.

`findings/index.json` keeps every signature, its runs, and its state. `findings/README.md` is the summary table. A signature already in the index is only counted again. Triage never changes the game.

The prototype's two overnight bugs went through this path: both reproduced, and both came with a draft, a 10 s clip, and the tuning noise in effect at the time.

## 6.5 Metrics

After each batch, M2 writes `metrics.json` grouped by profile × map × tuning hash × decider. Each metric has a per-run mean, bootstrap 95% interval (`ci95`), and its own measured-run count (`n`). The bootstrap seed and sample count are recorded. Missing observability is `mean: null`, `ci95: null`, `n: 0`, not a successful zero. One observation or constant observed values gives a zero-width bootstrap interval. That interval reflects the observed sample and does not prove population certainty, including when every sampled run extracts successfully.

The submitted batch inventory fixes the denominator. Missing folders, malformed telemetry, contract errors, divergence, crashes, timeouts, and invalid bridge summaries stay in `invalid_runs` and do not enter policy comparisons. A structurally valid oracle stop contributes to `oracle_rate`; survival and extraction are censored for that stopped run rather than counted as ordinary failure or success.

Game metrics use available telemetry: survival/extraction, session length, first contact, deaths per contact, emitted shots, healing use, and loot. First-observer and unseen-shooter metrics stay unmeasured when their evidence is absent. A shot count does not assume an arbitrary game consumes one ammunition unit per shot. Tinyraid explicitly emits supported zero counters in `run_end`, distinguishing a measured zero from unavailable instrumentation.

Framework metrics use decision receipts, tapes, and bridge summaries: decisions, dropped-decision rate, longest repeated tactic, fallback rate, cost, and latency. Cell latency p50/p95 values are means of each run's percentile, not percentiles pooled across all calls.

Paired comparisons match identical seed IDs within the same profile and map. They change either decider or tuning, never both at once, and report right-minus-left differences with paired bootstrap intervals. Seeds duplicated in a cell are listed as ambiguous and excluded from paired inference. These comparisons do not establish human fairness or M3 bug-detection recall.
