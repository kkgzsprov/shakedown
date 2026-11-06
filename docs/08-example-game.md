**English** | [한국어](ko/08-example-game.md)

# 8. Example game: tinyraid

`examples/tinyraid/` is a small real-time game that ships with Shakedown. It has three jobs: show a complete adapter, give contributors something to run with no engine installed, and act as a benchmark with known bugs so that changes to Shakedown can be measured.

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
| Rule decider | A short threshold rule, used for `rule` mode and fallback |
| Oracles | All of section 6.1, plus "door opened without key" |

It speaks the chapter 3 protocol over stdio, exactly as a game in another language would, so the bridge cannot tell it apart from a C++ or Godot game.

## 8.3 Seeded bugs

Each bug is off by default and switched on with `--bug <name>`. Each is meant to be caught by one specific mechanism, which lets the benchmark tell which part of Shakedown found it.

| Bug | What goes wrong | Should be caught by |
|---|---|---|
| `corner_pocket` | A wall corner pushes the player into a pocket it cannot leave | Stuck oracle |
| `thin_wall` | A wall with a collision gap lets a sprinting player pass through | Teleport or out-of-world oracle |
| `ammo_underflow` | Reloading while firing leaves the magazine at −1 | Numeric oracle |
| `door_no_key` | The locked door opens from the far side without the key | Game-specific oracle |
| `heal_overflow` | A heal at full health raises health above the maximum | Numeric oracle |
| `silent_grenade` | One guard's grenades make no warning sound | Anomaly: deaths by explosion with no warning |
| `extract_crouch` | Extraction never completes while crouched | Anomaly: timeouts inside the extraction zone |
| `vision_leak` | Guards behind walls appear in `seen` | Conformance check C7 |
| `wallclock_load` | A background "asset load" finishes on wall-clock time | Conformance check C1 or C3 fails under a slow decider |

`wallclock_load` reproduces the most likely determinism failure in real engines (section 3.10), so the fix can be shown in a small game first.

## 8.4 Benchmark

`shakedown bench tinyraid` runs, for each bug alone and for the clean game:

- A fixed exploration budget per seed set, with the `rule`, `random`, and `mock` deciders by default, and any configured model decider.
- Search seeds for development and held-out seeds for reporting.

It reports recall (bugs found and reproduced), precision (findings that match a seeded bug), false findings on the clean game, runs and cost until first detection, and which mechanism caught each bug.

A clean game with zero findings is the first acceptance condition. Shakedown's own changes are judged on held-out seeds. A run that tunes Shakedown on the same seeds it reports is not evidence.

tinyraid is a test bed, not a claim about real games. Results on it say whether Shakedown's mechanisms work, not how many bugs it will find in someone else's game.
