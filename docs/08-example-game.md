**English** | [한국어](ko/08-example-game.md)

# 8. Example game: tinyraid

`examples/tinyraid/` is a small real-time game that ships with Shakedown. It has three jobs: show a complete adapter, give contributors something to run with no engine installed, and act as a benchmark with known bugs so that changes to Shakedown can be measured.

M1 implements the headless game, adapter, local rule, input recording/replay, direct decision-tape replay, and optional PNG frames. M2 adds batches and conformance checks. M3 adds controlled input-workflow probes, anomaly evidence, verified triage, clips, and the benchmark below. M5 adds a separate planted-target balance benchmark; M6 adds Switchyard as a second, discrete-button adapter.

## 8.1 The game

- Top-down 2D, continuous positions, fixed 60 Hz simulation step, seeded.
- One player against a few guards. Walls, cover, loot crates, one locked door with a key, an extraction zone, and grenades.
- Guards have a vision cone, hearing, and a simple patrol-chase-search state machine.
- A session ends in death, extraction, or a time limit.
- Python standard library only. Headless by default. Optional frame dumps as PNG (written with `zlib`) for clips.

A Python game will not show every problem a real engine has, but it can model the important ones on purpose (section 8.3).

## 8.2 The adapter

The adapter is ordinary game code in `examples/tinyraid/adapter.py`, separate from the game rules so a reader can see where the boundary is:

| Part | tinyraid version |
|---|---|
| Observation | Self, equipment, guards inside the player's view cone with walls taken into account, memory of recently seen guards, noises, objective, recent tactics |
| Candidates | `engage_g<n>`, `cover_c<n>`, `withdraw`, `search_r<n>`, `heal`, `reload`, `loot_c<n>`, `use_door`, `extract`, `hold` |
| Executor | Movement along a grid A* path, aim with a reaction delay and seeded jitter, fire when the target is under the cursor |
| Reflexes | Re-aim on target change, leave a grenade's radius after the reaction time |
| Rule decider | A short threshold rule for `rule` mode; the hosted-backend fallback is planned |
| Oracles | Per-step stuck, teleport, numeric, player/NPC bounds, solid collision, and "door opened without key" checks; no-progress warnings. M2 records crashes; M3 triage checks reproduction when faithful source evidence is available |

It speaks the chapter 3 protocol over stdio, exactly as a game in another language would, so the bridge cannot tell it apart from a C++ or Godot game.

## 8.3 Seeded bugs

Each bug is off by default and switched on with `--bug <name>`. Each is meant to be caught by one specific mechanism, which lets the benchmark tell which part of Shakedown found it.

| Bug | What goes wrong | Should be caught by |
|---|---|---|
| `corner_pocket` | A wall corner pushes the player into a pocket it cannot leave | Stuck oracle |
| `thin_wall` | A wall with a collision gap lets a sprinting player pass through | Solid-collision oracle (`solid_collision`), even when the player stays inside map bounds |
| `ammo_underflow` | Reloading while firing leaves the magazine at −1 | Numeric oracle |
| `door_no_key` | The locked door opens from the far side without the key | Game-specific oracle |
| `heal_overflow` | A heal at full health raises health above the maximum | Numeric oracle |
| `silent_grenade` | One guard's grenades make no warning sound | Anomaly: deaths by explosion with no warning |
| `extract_crouch` | Extraction never completes while crouched | Anomaly: timeouts inside the extraction zone |
| `vision_leak` | Guards behind walls appear in `seen` | Conformance check C7 |
| `wallclock_load` | A background "asset load" finishes on wall-clock time | Conformance check C1 or C3 fails under a slow decider |

Controlled scenarios isolate the input triggers; enabling a bug alone does not guarantee a rule bot will visit it. Tests exercise these fixtures directly. The `wallclock_load` injection deliberately consults the host clock: host timing changes the `asset_loaded` telemetry event, so C3's telemetry equality check fails even when the decision hashes still match. It is a negative determinism fixture, not a supported input-replay workload.

## 8.4 Benchmark

Run the full controlled holdout benchmark from the repository root after the editable install in the root README:

```bash
python -m shakedown bench tinyraid --output runs/tinyraid-bench
```

`benchmark.json` and `benchmark.md` distinguish complete qualification coverage from a passing gate. The primary suite uses `yard`, the `cautious` profile, all three offline policies (`rule`, `random`, `mock`), and seven input workflows. Every clean/single-fault condition gets the same fixed plan, normalized plan hash, and budget. Detectors inspect oracle and telemetry evidence; only the scorer applies expected fault labels after detection and triage.

The frozen `m3-seeds.json` separates search seeds `[101, 211, 307]` from holdout seeds `[1009, 2003, 3001, 4001, 5003, 6007]`. Default holdout coverage is 7 workflows × 3 policies × 6 seeds = 126 attempts per condition, or 1,008 clean-plus-seven-fault attempts before reproduction and diagnostics. Each cell exhausts its seed pool before repetition.

| Workflow | Ordinary input or behavior under test |
|---|---|
| `navigate_corner` | The normal adapter navigates a corner fixture |
| `sprint_wall` | Sprint against a wall, then stop attempting movement |
| `reload_fire` | Request reload and fire together through the regular input path |
| `heal_top_up` | Request healing at full health |
| `unlock_door` | Try the far side of a locked door without a key |
| `grenade_escape` | Suppress ordinary walking and preserve the cautious-profile hazard reflex |
| `crouched_extract` | Attempt extraction while crouched |

These are declared controlled probes, not autonomous discovery in ordinary gameplay. Input generation never branches on the planted-fault switch. Clean and fault runs use the same workflow; independent input replay bypasses probe generation.

Primary recall covers five oracle faults and two anomaly faults. The anomaly gate fixes a minimum of five measured runs, thresholds of 0.25 for unwarned explosion deaths and extraction-zone timeouts, and a measured clean baseline. Other generic thresholds are fixed at 1.0 in this test bed because intentional probe waits are not timeout bugs. Generic exploration retains ordinary defaults. Reports include signature-based precision, independently reproduced recall, clean findings, actual fixed-budget consumption, and the mechanism. The first-detectable run is reconstructed retrospectively from the completed budget, not an observed online stopping time. Offline service cost is zero.

Qualification requires the full holdout seeds, policies, budget, seven primary cases, and both conformance diagnostics. Passing additionally requires equal plans, zero clean findings/invalid executions, and every primary fault matched and independently reproduced. Search or partial/debug coverage is unqualified even if a selected fault reproduces. `vision_leak` (C7) and `wallclock_load` (C3 with a controlled child host clock) are separate diagnostics and do not inflate primary recall. The clock diagnostic does not claim input-replay support for deliberately nondeterministic work.

These results measure Shakedown mechanisms on this fixture suite. They do not estimate unknown-game bug discovery, and a result tuned on the reporting seeds is not independent holdout evidence.

## 8.5 Running and checking M1–M3

Run the checkout commands in the [root README](../README.md#try-it) from the repository root. `rule`, `bridge`, and `tape:<file>` select decision paths; `--replay-inputs` bypasses all three and loads the complete recorded startup configuration. The `cautious`, `aggressive`, and `looter` profiles set reaction/decision delays and aiming jitter. Both `yard` and `warehouse` maps are available.

`examples/tinyraid/m1-seeds.json` fixes the clean acceptance matrix: seeds 1, 7, and 42 × two maps × three profiles, with a 45-second limit per run (18 runs). M1 acceptance requires C1–C6 and no oracle, contract, divergence, or crash findings in this matrix. Death, extraction, and ordinary timeout remain valid game outcomes. This finite development matrix does not establish M3 held-out benchmark recall.

The tests also cover a C7 wall-visibility fixture, resource conservation, per-step movement/collision, seeded numeric/door/geometry bugs, and delayed decision receipts. Tinyraid's stuck check uses a rolling 10-second window with movement attempted on at least 80% of steps and less than 0.5 m cumulative movement, so brief replanning pauses do not hide a trapped player. The forced-stuck scenario writes a recording that reproduces the oracle with exit code 4.

M2's `examples/tinyraid/shakedown.toml` adds a distinct 60-run batch (seeds 0–9 × two maps × three profiles, `rule`, 12 seconds each). Use `--seconds 45` for a longer limit. The root README shows a six-run smoke override and the generic `check-adapter` command. The supplied oracle fixture forces `stuck`; the visibility fixture tests a hidden guard over multiple steps and includes a visible positive control. Both checks keep replay/process evidence. These are conformance checks, not M3 exploration recall.

For frame dumps, with no graphics dependency or display server:

```bash
python -m examples.tinyraid --seed 7 --duration 12 --frame-dir runs/tinyraid-frames --frame-every 60
```

`--tuning` accepts a JSON object of values, or a `tuning_version: 1` envelope with a `values` object. Unknown keys and values outside these ranges exit 2; omitting the file preserves defaults:

| Key | Default | Range |
|---|---|---|
| `player.speed` | 3.0 m/s | 1.0–5.0 |
| `guard.damage` | 8.0 | 1.0–20.0 |
| `guard.vision` | 10.0 m | 3.0–16.0 |

M3 records the optional `probe` name and actual startup scenario. Older seven-field recording configurations remain accepted, but strict game-event hashes still apply. Earlier telemetry or engine semantics are not automatically byte-compatible; an assertion mismatch is a failure, not a claimed migration.

## 8.6 Planted balance target

`bench tinyraid-balance` is a separate fixed-plan numeric-search test, not the M3 fault benchmark:

```bash
python -m shakedown bench tinyraid-balance --output runs/tinyraid-balance-bench
```

It freezes the seed sets in `examples/tinyraid/m5-seeds.json`, searches only `player.speed`, and holds map, duration, instructions, source hashes, and policies fixed. The primary `rule` policy must recover the synthetic loot target 55–60 on holdout seeds. `mock` independently checks preference reversal and may remain outside the target range; a passing gate does not claim all-policy target attainment or human balance. The benchmark writes `benchmark.json` plus the normal campaign evidence and un-applied proposal patch.

The search/holdout split and planted objective are qualification design inputs, not evidence of a passing run. Inspect `gate_passed`, primary-rule target distances, complete samples, oracle comparisons, and the frozen selection in the retained output. A weaker target whose baseline already satisfies the range must produce no gain proposal. Tinyraid reads `tuning_keys.json` as its runtime default source; benchmark proposals never edit it.

## 8.7 Switchyard and skill verification

`examples/switchyard/` is a deterministic turn-based parcel route puzzle on `depot` and `loop`, with `courier` and `careful` profiles. Its thin executor passes each selected action through an ordinary button to `Game.tick`; input replay reconstructs startup state without the adapter. The configuration includes an oracle fixture and a hidden-traffic visibility check.

```bash
python -m shakedown check-adapter --config examples/switchyard/shakedown.toml --output runs/switchyard-checks
python -m shakedown run --config examples/switchyard/shakedown.toml --output runs/switchyard-batch
```

Use new output directories. These checks retain separate C1–C7 verdicts; the batch covers both maps, both profiles, three seeds, and `rule`/`mock`. This second example exercises the [agent skill](../skill/shakedown/SKILL.md) verification workflow. The shipped implementation preceded the skill read. In a separate fresh-context trial, an agent read only the skill, game rules, protocol, and configuration, then wrote an independent adapter without reading the shipped adapter. Seed 7/depot/courier passed C1–C6, meeting the minimum C1–C3 criterion; C7 remained `unmeasured` and the overall seven-check verdict therefore remained unmeasured. This is scoped blind-adapter evidence, not broad unseen-game generalization. Neither the puzzle nor conformance establishes real-time aiming, human-like play, or visual quality.
