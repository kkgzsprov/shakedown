**English** | [한국어](ko/07-balance-loop.md)

# 7. Balance loop

The M5 balance loop produces evidence for a human design decision. `campaign --optimize` searches tuning values against human-approved target ranges and proposes a change only after it holds up on seeds the search never saw. Without `--optimize`, `campaign` retains the M4 single baseline round. Neither mode applies defaults on its own; `shakedown apply` writes a reviewed proposal to the key table, and only under `--approved`.

## 7.1 Tuning key table

The game exposes tunable values through a key table. Shakedown reads `tuning.keys` relative to the configuration file. M5 requires a documented `default` or `baseline` for every key so it can construct a complete fixed baseline (keys and ranges below are illustrative):

```json
{
  "keys": {
    "squad.fireTokens":        {"type": "int",   "min": 1,   "max": 4,   "default": 3,   "unit": "count"},
    "squad.tokenReturnSeconds":{"type": "float", "min": 0.2, "max": 1.5, "default": 0.7, "unit": "s"},
    "detection.baseRange":     {"type": "float", "min": 20,  "max": 70,  "default": 40,  "unit": "m"}
  }
}
```

A run's tuning file holds values only:

```json
{"tuning_version": 1, "values": {"squad.fireTokens": 2, "detection.baseRange": 30}}
```

The adapter rejects unknown keys and out-of-range values with exit code 2. A missing tuning file must leave every default unchanged. The conformance suite checks that the game's key table and `tuning_keys.json` agree.

Tinyraid loads this table as the actual source of its bounds and defaults. For other games, the table may only document defaults: verify how the game consumes them before applying a proposed patch. A proposal changes numeric default fields in the table, not arbitrary game source code. The table must be inside the configured project root.

Choose keys the game actually reads at the point that matters. In the prototype, per-weapon parameters overwrote two cone keys, so they were replaced by one multiplier applied to every enemy burst. That multiplier is applied before a cap, so values above 1 are clipped at long range. The key table should record such limits.

## 7.2 The loop

1. The planner drafts `targets.json`. A human edits and approves it.
2. The planner writes `campaign.json`. A human approves it.
3. The orchestrator measures the documented baseline, then samples candidates with standard-library random or lazy grid search. Every experiment must use the campaign's global `search_ranges`. It takes the first `batch_size` search seeds from each experiment and merges duplicate profile/map scopes without duplicating their seeds. Every scope needs at least `min_runs` unique seeds.
4. Each round aggregates metrics and anomaly signals. The default is a fixed schedule using the approved campaign's `rounds`. With `planner_revisions = true`, the configured planner can stop or revise numeric ranges. Revisions are clipped to the approved search box and logged; they cannot introduce keys or change policy instructions. Human/model labeling is not supplied automatically.
5. Search metrics rank the candidates. `selection.json` freezes the top `top_k` measured improvements before any holdout run. Only then do the baseline and those candidates run once on the reserved holdout set. If search has no measured improvement, the holdout set remains unused.
6. The deterministic gate (section 7.4) controls patch output. `report.md` records the campaign evidence, and `planner-report/` contains a supplemental planner draft. A human decides whether to apply a proposal with `apply` (section 7.4).

Add a `[balance]` table before approving targets and the campaign. This example uses seeds outside the bundled configuration's search set; choose a fresh holdout set for your own experiment:

```toml
[balance]
method = "grid"
seed = 0
candidates_per_round = 4
grid_points = 3
holdout_seeds = [10007, 10009, 10037, 10039, 10061]
min_runs = 5
min_gain = 0.2
top_k = 1
max_runs = 1000
policies = ["rule", "mock"]
planner_revisions = false
policy_files = ["examples/tinyraid/__main__.py", "examples/tinyraid/game.py", "examples/tinyraid/adapter.py", "examples/tinyraid/probes.py"]
```

`method` accepts `random` or `grid`. The shown settings are defaults except `method`, the required holdout set and `policy_files`, and the explicit policies. `policy_files` must name the game's executor and policy sources relative to the project root; M5 requires a nonempty list of files inside that root and freezes their hashes along with the bridge, decider, and runtime sources. Policies otherwise come from `batch.deciders` and must be offline `rule`, `mock`, or `random`. The holdout set must contain at least `min_runs` unique integers and be disjoint from **all** configured batch seeds, even search seeds a smaller batch does not use. The admission check compares the worst-case baseline/search/holdout run count against the cap before creating output or starting children. `--max-runs` can lower `balance.max_runs`, never raise it. Changing relevant settings requires new approval receipts.

Use the same reviewed document and receipt paths as the M4 workflow, with the extra mode flag:

```text
shakedown campaign --config CONFIG --campaign CAMPAIGN_JSON --approval CAMPAIGN_RECEIPT --targets TARGETS_JSON --targets-approval TARGETS_RECEIPT --output OUTPUT_DIR --approved --optimize
```

Uppercase values are operator-supplied paths, not literal filenames. Use a new output directory. `balance_run.json` retains the run budget, hashes, phases, revisions, selection, gates, and proposal paths. Search and holdout metrics are separate. A completed campaign can have no proposal because its gate was rejected or unmeasured; inspect the gate rather than treating CLI success as a balance verdict.

## 7.3 Holding the bot fixed

While comparing tuning values, keep the bot policy fixed: decider kind, model ID, profile instructions, and executor profile. If the bot changes while tuning changes, nobody can tell which moved the metrics. Changing profile instructions starts a new campaign.

M5 records a policy fingerprint and checks that approved documents, configuration, defaults, instruction files, `policy_files`, declared fixtures, and bridge/protocol/decider sources have not changed between phases. It runs the same named offline policies, duration, and approved profile instructions for baseline and candidates. A fingerprint does not establish the behavior of undeclared external game code; list all relevant game policy sources and keep the runtime fixed.

The prototype applied this rule to its own changes: after adding `recent` outcomes, the grenade reflex, and looting, it stopped comparing new runs with the overnight run.

Bots that are tuned for will drift from human play. Three defenses:

- Several profiles (for example cautious, aggressive, looter) rather than one.
- The same telemetry on human sessions, so distributions can be compared (experiment E5).
- **Policy sensitivity.** M5 requires at least two distinct tested policies for a proposal. If one improves the target-distance score while another worsens it, the candidate is rejected as policy-sensitive. Agreement between `rule` and `mock`, for example, establishes only those policies' observed preference. Human play and hosted-model agreement remain unmeasured; a single policy cannot certify sensitivity.

## 7.4 The proposal gate

A candidate becomes a proposed change only if all of these hold on holdout seeds:

- Its raw distance outside the target ranges, summed over expanded target/profile/map cells and tested policies, is at most (1 − `min_gain`) of the baseline's, and strictly improves it. The default is `min_gain = 0.2`. A zero-distance baseline produces no change proposal.
- Its oracle rate is no higher than the baseline's in every profile/map/policy group. Oracle measurements must cover the full group denominator.
- Every baseline/candidate group has at least `min_runs` valid runs, and every target metric has at least that many measured observations. Missing, duplicate, censored, nonfinite, or insufficient evidence stays `unmeasured`; invalid executions block the proposal.
- At least two named policies have measured scores and no observed preference reversal.

A passing candidate writes `proposals/pNNN/defaults.patch`, `tuning.json`, and `proposal.json`. The receipt includes the source hash, numeric default changes, and gate details with scores and per-target values. The patch targets the documented key table; the source file stays unchanged. Bootstrap intervals are descriptive evidence, not a statistical guarantee of gain.

`shakedown apply --config CONFIG --proposal PROPOSAL_DIR` verifies a proposal without writing: the receipt must be unapplied and from a passed gate, the values must match the gated candidate, the live table must be the configured `tuning.keys` file with the recorded source hash, and the patch must reproduce byte-for-byte from the table and values. With `--approved` it then writes the new defaults and marks the receipt `applied` with before/after hashes, so the same proposal cannot be applied twice. Rejected verification exits nonzero before any write. Build/test/rollback and committing the table remain operator steps, not implemented commands.

## 7.5 What the targets should not be

Do not maximize one number. A target that only raises survival is met by removing damage. Use ranges on several metrics at once, per profile and map: survival, reward, deaths per contact, time from first warning to first hit, engagement length, ammo and healing use, stuck and timeout rates.

Target ranges are a design choice. Before humans have set them, read results as comparisons between candidates, not as a balance verdict.
