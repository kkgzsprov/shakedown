---
name: shakedown
description: Add a deterministic game adapter, reproduce playtest findings, and run approved balance campaigns with Shakedown evidence and replay artifacts.
---

# Shakedown

Use the game's normal input path to play and replay. Keep the planner between runs, the decider at discrete decision points, and the executor inside the game. Start with offline `rule` and `mock` runs; a hosted backend needs an explicitly configured account and budget.

## Adapt a game

Read [the adapter guide](references/adapter-guide.md) when adding or repairing an adapter. It gives the implementation order, wire contract, evidence files, and C1–C7 acceptance checks. For a game whose actions are individual buttons or API calls, use [the thin executor guide](references/thin-executor.md).

Language starting points are [Python](assets/adapter_python.py), [C++17](assets/adapter_cpp17.hpp), [C#](assets/Adapter.cs), and [Godot 4 GDScript](assets/adapter.gd). They are transport skeletons, not complete game adapters. Python transport behavior is tested, and C# has a .NET 8 build/wire smoke check. C++17 and GDScript remain uncompiled. Every language still needs game-owned input/replay integration and real conformance evidence. Use [the visibility fixture pattern](assets/visibility_fixture.py) to build a game-owned C7 test.

Run `check-adapter` against real game processes. C1–C3 passing defines support; C4–C7 retain separate verdicts. Missing fixtures or observations are `unmeasured`, never a pass. Do not substitute a template test for game conformance.

## Playtest and reproduce

Use `run` for a fixed batch, `explore` for bounded QA sampling, and `triage` for independent reproduction. Keep each invocation in a new output directory. Inspect invalid executions, oracle findings, anomaly signals, and reproduction verdicts separately. Report only what retained telemetry, recordings, and replay receipts establish. A planner's suspected cause remains a hypothesis; a bot's opinion is not a fairness verdict.

Run commands with the installed `shakedown` entry point or this portable wrapper:

```text
python scripts/run_shakedown.py check-adapter --config GAME_CONFIG --output NEW_CHECK_DIR
python scripts/run_shakedown.py explore --config GAME_CONFIG --output NEW_QA_DIR --runs 20
python scripts/run_shakedown.py triage --config GAME_CONFIG --batch QA_DIR --output NEW_TRIAGE_DIR
```

Paths above are caller-supplied paths. Invoke the wrapper by its real location; it keeps the caller's working directory and passes arguments without a shell. Use `--repo SHAKEDOWN_CHECKOUT` before the command if the skill is copied away from its source checkout.

## Plan and balance

`plan targets` and `plan campaign` produce drafts. Use the named input `design_notes`, and inspect the generated JSON before recording operator approval with `approve`. Profile instructions are limited to 600 characters each and cannot introduce new actions. Follow existing user authorization; a generated draft cannot approve itself. Receipts bind the exact document, tuning table, and execution settings.

`campaign --optimize` searches only approved numeric ranges. Keep the policy, profile instructions, executor code, and simulation duration fixed. Supply disjoint search/holdout seeds, source files in `balance.policy_files`, and an execution budget. Freeze candidate selection before inspecting holdout results. `balance_run.json`, `selection.json`, per-batch metrics, holdout gates, and `report.md` explain the outcome. A missing policy comparison or target measurement blocks a proposal.

Proposed defaults remain reviewable artifacts. Do not apply or commit a balance patch as part of playtesting. Present the patch and its evidence for the user's design decision. Do not write API keys into game files, configuration, tapes, reports, or skill assets. Planner and decider cost budgets are separate; unknown costs are not zero.
