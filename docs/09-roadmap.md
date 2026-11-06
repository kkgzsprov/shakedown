**English** | [한국어](ko/09-roadmap.md)

# 9. Roadmap, experiments, and open questions

## 9.1 Milestones

Each milestone ends with a check that passes on CI with no network and no key.

Current implementation: M0–M3. M3 adds seeded coverage exploration, opportunity-based anomaly reports, persistent reproduction-backed triage, replay-verified clips, and the controlled `bench tinyraid` command. The primary benchmark separates search and holdout seeds and requires zero clean findings plus reproduced oracle/anomaly faults. M4 and later remain planned. Local validation and current-commit CI are separate evidence; inspect the actual run before claiming another platform passed.

| Milestone | Contents | Done when |
|---|---|---|
| M0 Protocol | `protocol.py`, `bridge.py`, `random`/`mock`/`tape` deciders, a fake game for tests | Contract-error and divergence tests pass. Request hashing is stable across Python versions on CI. |
| M1 tinyraid | Game, adapter, `rule` decider, oracles, telemetry, input recording and replay | C1 to C6 pass on tinyraid. The clean game gives no findings over the M1 seed set. |
| M2 Batch runs | `shakedown.toml`, orchestrator, workers, aggregation, `check-adapter` | 60-run batch on tinyraid produces `metrics.json` with intervals. `check-adapter` reports C1 to C7. |
| M3 QA path | Exploration, anomalies, triage, clips | `shakedown bench tinyraid` catches each oracle and anomaly bug alone and reproduces it, on held-out seeds |
| M4 Planner | Planner steps and schemas, `claude-cli`, `codex-cli`, `mock` backends, approval gates | Both CLI backends pass every schema in a manual run. Mock passes on CI. |
| M5 Balance | Key table, optimizer, campaign loop, holdout gate, patch output | A planted tuning target on tinyraid is recovered within its range on holdout seeds |
| M6 Skill | `skill/shakedown/`, adapter templates, thin-executor mode | An agent with the skill writes a passing adapter for a second small game that it has not seen before |
| M7 Hosted decider | `jev` backend, cache, budget, invalid-run rules | Latency, cost, and fallback reported for a tinyraid batch |

M4 can be built in parallel with M2 and M3, using mock metrics. M7 is last on purpose: nothing before it needs a paid service.

## 9.2 Experiments before a 1.0 release

These test whether Shakedown's bots are good enough to base decisions on. They come from the prototype's design and had not been run when this was written.

| Experiment | Comparison | Report |
|---|---|---|
| E0 Determinism | Same arguments twice in `rule`; once with a slow model decider, then its tape | Byte-identical telemetry for each pair |
| E1 Decider value | Random vs rule vs model decider, same candidates and seeds, each profile | Whether the model beats random on survival and deaths per contact. The result against the rule is reported either way. |
| E2 Profile separation | Model decider with only the profile instructions changed | Whether tactic mix, engagement range, and session length differ by profile. If not, the planner → decider path does not work. |
| E3 Name and order sensitivity | The same 200 decisions re-asked with shuffled order and neutral names (`cover_c1` → `opt_3`) | Agreement of the most frequent choice |
| E4 Delay sensitivity | `decision_delay` 0.1, 0.3, 0.6 s | How much metrics move. Balance runs fix one value. |
| E5 Human comparison | Telemetry from human sessions vs each profile | Which profile is closest to people, used as the first reference for balance |

If E1 shows the model is no better than the rule, the balance loop runs on the rule, and the model is used only for labeling. That outcome is still a useful result.

## 9.3 Later

- Local open decision models as deciders (Laya, SemIf, or others), so a full campaign can run offline.
- Engine adapters maintained in this repository: Godot first, then Unity and Unreal, if contributors want them.
- A model-based death labeler with separate calibration and test sets. Thresholds are set on the calibration set, accuracy is reported on the test set, and frames from one engagement never go into both.
- Video clips through a configurable encoder instead of Blender.

## 9.4 Open questions

| Question | Why it matters |
|---|---|
| License | Not chosen yet. It decides whether studios can embed the adapter templates in closed games. A permissive license (MIT or Apache-2.0) fits that use. |
| Protocol versioning | How a bridge handles an adapter on an older `v`. For now the bridge rejects any `v` other than 1 as a contract error. |
| Rate limits of hosted deciders | Not documented for Jev. They set the worker count for M7. |
| Multiplayer and co-op games | One bot per process for now. Several bots in one session would need one request per bot per decision point. |
| Non-pausable games | Network games with authoritative servers cannot stop the clock. Supporting them would mean giving up lockstep replay. |
