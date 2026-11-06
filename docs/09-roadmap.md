**English** | [한국어](ko/09-roadmap.md)

# 9. Roadmap, experiments, and open questions

## 9.1 Milestones

Each milestone ends with a check that passes on CI with no network and no key.

Current implementation: M0–M7 paths. M4 adds structured planning, approval, and budget accounting; M5 adds numeric search, frozen holdout gates, and reviewable patches; M6 adds the agent skill and second example adapter; M7 adds Jev HTTP decisions. Implementation, offline fixtures, live providers, scoped blind adaptation, and human-play validation are separate claims. Live planner/provider calls have not established production behavior here. Local validation and current-commit CI are also separate evidence; inspect the actual run before claiming another platform passed.

| Milestone | Contents | Done when |
|---|---|---|
| M0 Protocol | `protocol.py`, `bridge.py`, `random`/`mock`/`tape` deciders, a fake game for tests | Contract-error and divergence tests pass. Request hashing is stable across Python versions on CI. |
| M1 tinyraid | Game, adapter, `rule` decider, oracles, telemetry, input recording and replay | C1 to C6 pass on tinyraid. The clean game gives no findings over the M1 seed set. |
| M2 Batch runs | `shakedown.toml`, orchestrator, workers, aggregation, `check-adapter` | 60-run batch on tinyraid produces `metrics.json` with intervals. `check-adapter` reports C1 to C7. |
| M3 QA path | Exploration, anomalies, triage, clips | `shakedown bench tinyraid` catches each oracle and anomaly bug alone and reproduces it, on held-out seeds |
| M4 Planner | Planner steps and schemas, `claude-cli`, restricted opt-in `codex-cli`, and `mock`; budget ledger, approval receipts, one baseline campaign round | Offline schema, semantic, budget, approval, subprocess-fixture, and mock campaign tests pass. Both real CLI backends still require separately budgeted manual schema qualification; restricted Codex is not certified tool-free. |
| M5 Balance | Implemented key-table defaults, random/grid optimizer, bounded campaign loop, holdout gate, and reviewable patch output | Qualify a planted tinyraid target on untouched holdout seeds; gate and loop tests must cover missing evidence, policy disagreement, budgets, and stale approvals. Implementation alone is not this qualification evidence. |
| M6 Skill | Implemented `skill/shakedown/`, transport skeletons, adapter guides, wrapper, and Switchyard thin-executor example | Shipped-adapter conformance and a separate fresh-context independent adapter are distinct evidence. The trial met C1–C3 (also C4–C6) on seed 7/depot/courier; C7 was unmeasured. Broader unseen-game generalization remains unproven. |
| M7 Hosted decider | Implemented pinned `jev` HTTP backend, shared cache/budget/concurrency, bounded retries, and invalid-run receipts | Local HTTP fixtures verify the transport path; live inference, production latency, actual charge, and model-value comparisons remain unmeasured. |

M4 can be validated with mock metrics and subprocess fixtures without a paid service. Its real CLI backend qualification is a separate manual gate with an explicit call budget. M5 compares only its named offline policies; this does not establish agreement with hosted models or human play. M7's token estimates and reservation ledger do not establish the provider's actual bill. No implemented command automatically applies or commits proposed defaults.

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
| Protocol versioning | How a bridge handles an adapter on an older `v`. For now the bridge rejects any `v` other than 1 as a contract error. |
| Rate limits of hosted deciders | Not documented for Jev. They set the worker count for M7. |
| Multiplayer and co-op games | One bot per process for now. Several bots in one session would need one request per bot per decision point. |
| Non-pausable games | Network games with authoritative servers cannot stop the clock. Supporting them would mean giving up lockstep replay. |
