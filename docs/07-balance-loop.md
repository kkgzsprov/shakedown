**English** | [한국어](ko/07-balance-loop.md)

# 7. Balance loop

The balance loop produces evidence for a human design decision. It searches tuning values against human-approved target ranges and proposes a change only after it holds up on seeds the search never saw.

## 7.1 Tuning key table

The game exposes tunable values through a key table. Shakedown keeps a copy as `tuning_keys.json` (keys and ranges below are illustrative):

```json
{
  "keys": {
    "squad.fireTokens":        {"type": "int",   "min": 1,   "max": 4,   "unit": "count"},
    "squad.tokenReturnSeconds":{"type": "float", "min": 0.2, "max": 1.5, "unit": "s"},
    "detection.baseRange":     {"type": "float", "min": 20,  "max": 70,  "unit": "m"}
  }
}
```

A run's tuning file holds values only:

```json
{"tuning_version": 1, "values": {"squad.fireTokens": 2, "detection.baseRange": 30}}
```

The adapter rejects unknown keys and out-of-range values with exit code 2. A missing tuning file must leave every default unchanged. The conformance suite checks that the game's key table and `tuning_keys.json` agree.

Choose keys the game actually reads at the point that matters. In the prototype, per-weapon parameters overwrote two cone keys, so they were replaced by one multiplier applied to every enemy burst. That multiplier is applied before a cap, so values above 1 are clipped at long range. The key table should record such limits.

## 7.2 The loop

1. The planner drafts `targets.json`. A human edits and approves it.
2. The planner writes `campaign.json`. A human approves it.
3. The orchestrator runs each tuning candidate over N seeds × profiles × maps. The default is N = 20. Seeds are split into search seeds and holdout seeds, and holdout seeds are used only at the end.
4. Aggregate → label → planner `revise` → the optimizer proposes the next candidates inside the key table. The first optimizer is standard-library random and grid search.
5. After the set number of rounds, the top candidates rerun on holdout seeds, and the planner writes `report.md`.
6. Shakedown writes a patch for any candidate that passes the gate (section 7.4). A human decides whether to apply it.

## 7.3 Holding the bot fixed

While comparing tuning values, keep the bot policy fixed: decider kind, model ID, profile instructions, and executor profile. If the bot changes while tuning changes, nobody can tell which moved the metrics. Changing profile instructions starts a new campaign.

The prototype applied this rule to its own changes: after adding `recent` outcomes, the grenade reflex, and looting, it stopped comparing new runs with the overnight run.

Bots that are tuned for will drift from human play. Three defenses:

- Several profiles (for example cautious, aggressive, looter) rather than one.
- The same telemetry on human sessions, so distributions can be compared (experiment E5).
- **Policy sensitivity.** If the `rule` decider and a model decider disagree about which tuning candidate is better, the candidate is marked policy-sensitive and is not proposed until human play confirms it.

## 7.4 The proposal gate

A candidate becomes a proposed change only if all of these hold on holdout seeds:

- Its distance outside the target ranges, summed over targets and profiles, is at most (1 − `min_gain`) of the baseline's. The prototype default was `min_gain` = 0.2.
- Its oracle rate is no higher than the baseline's.
- Every group has at least `min_runs` runs.
- It is not policy-sensitive.

The output is a patch to the game's defaults plus a report with the scores and the per-target values. If the project opts in to automatic application (section 5.4), Shakedown builds and runs the game's tests before and after the edit and restores every file on any new failure.

## 7.5 What the targets should not be

Do not maximize one number. A target that only raises survival is met by removing damage. Use ranges on several metrics at once, per profile and map: survival, reward, deaths per contact, time from first warning to first hit, engagement length, ammo and healing use, stuck and timeout rates.

Target ranges are a design choice. Before humans have set them, read results as comparisons between candidates, not as a balance verdict.
