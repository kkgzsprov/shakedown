**English** | [한국어](ko/08-example-game.md)

# 8. Example game: tinyraid

`examples/tinyraid/` is a small real-time game that ships with Shakedown. It has three jobs: show a complete adapter, give contributors something to run with no engine installed, and act as a benchmark with known bugs so that changes to Shakedown can be measured.

M1 implements the headless game, adapter, local rule, input recording/replay, direct decision-tape replay, and optional PNG frames. The exploration/triage benchmark in section 8.4 is planned for M3.

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
| Oracles | Per-step stuck, teleport, numeric, player/NPC bounds, solid collision, and "door opened without key" checks; no-progress warnings. M2 records crashes; automatic reproduction belongs to future triage |

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

The planned M3 command `shakedown bench tinyraid` will run, for each bug alone and for the clean game:

- A fixed exploration budget per seed set, with the `rule`, `random`, and `mock` deciders by default, and any configured model decider.
- Search seeds for development and held-out seeds for reporting.

It reports recall (bugs found and reproduced), precision (findings that match a seeded bug), false findings on the clean game, runs and cost until first detection, and which mechanism caught each bug.

A clean game with zero findings is the first acceptance condition. Shakedown's own changes are judged on held-out seeds. A run that tunes Shakedown on the same seeds it reports is not evidence.

tinyraid is a test bed, not a claim about real games. Results on it say whether Shakedown's mechanisms work, not how many bugs it will find in someone else's game.

## 8.5 Running and checking M1–M2

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
