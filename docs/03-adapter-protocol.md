**English** | [한국어](ko/03-adapter-protocol.md)

# 3. Adapter and wire protocol

The adapter is the game-side half of Shakedown. It must live in the game because only the game can pause its own clock, compute what is legal, and inject input through the player's input path. Shakedown supplies the protocol, a conformance checker, and a reference adapter (chapter 8). The agent skill (chapter 5) helps a coding agent write an adapter for a new game.

## 3.1 Responsibilities

| Part | Must do |
|---|---|
| Headless run mode | Run one session from a seed with a fixed simulation step, no wall-clock timing, no real input |
| Observation | Serialize what the player could know, and nothing more |
| Candidates | List the actions that are legal right now, each with a short factual description |
| Decision points | Decide when to ask, pause the clock, send the request, wait for the answer |
| Executor | Turn the chosen action into input events every step until it ends, fails, or is replaced |
| Reflexes | React within one step to dangers that cannot wait for a decision |
| Oracles | Check invariants every step and stop the run on a violation |
| Telemetry | Write events as JSON Lines |
| Input recording | Write every injected input event so the run replays without a decider |

## 3.2 Run modes

The adapter accepts a decider mode through its command line (the name of the flag is up to the game, see section 2.4):

| Mode | Who decides | Use |
|---|---|---|
| `rule` | A rule inside the game | Baseline, CI smoke tests, runs with no bridge |
| `bridge` | The Shakedown bridge over stdio | Experiments with any Python decider |
| `tape:<file>` | Recorded answers, in order | Reproduction. A request whose hash differs from the tape is a divergence |

A `rule` decider is optional but cheap, and it gives every game a no-network baseline. Separately, the adapter must replay `inputs.json` with no decider at all.

Each run gets a fresh copy of any save file or profile the game reads at startup. The starting file is a fixture checked into the game's repository, and `run.json` records its name.

## 3.3 Observation

Give the bot only what the player could know. An enemy-side world model usually holds true positions, and serializing it leaks information the player never had.

| Group | Examples |
|---|---|
| Self | Position, health by body part, status effects, stance, stamina |
| Equipment | Weapon, rounds loaded, spare magazines, consumables |
| Seen | Enemies or objects currently visible on the player's screen: relative bearing, distance, exposure, seconds since first seen |
| Memory | Last known positions of things seen recently, with elapsed time |
| Cues | Sounds, damage-direction indicators, HUD warnings |
| Objective | Current goal, route distance, time left in the session, time left in the planned exploration phase |
| Recent | The last few tactics with how each ended: `done`, `failed`, `replaced`, or `dropped`, plus seconds and metres moved |

Compute every number in the adapter: distances, route lengths, line of sight, cover scores. Do not ask the decider to do arithmetic.

Two prototype lessons belong here. Without `objective.time_left`, the decider could not tell exploring from extracting (section 1.5). Without `recent`, it kept choosing the same failed tactic every 0.5 s, and both bugs from the overnight run were that loop. Use integers or rounded values in the observation so that the request hash stays deterministic.

What counts as "visible" needs care. In the prototype, the game's own visibility flag stayed true until the first line-of-sight check ran, and that check also passed bodies that were off screen. The adapter therefore counted an enemy as seen only when its chest projected inside the output frame. Each adapter should add a unit test that an entity behind a wall does not appear in `seen`.

## 3.4 Candidates

At each decision point the adapter lists at most 12 legal candidates. Each candidate is an action the executor can carry through for a few seconds.

| Kind | Example name | Description holds |
|---|---|---|
| Engage | `engage_e2` | Target, distance, exposure, rounds in the magazine |
| Move to cover | `cover_c1` | Distance, exposed seconds on the way, which threats it blocks |
| Withdraw | `withdraw` | Direction and route length |
| Search | `search_r4` | Room, whether visited, whether a cue points there |
| Heal | `treat_bleed_legs` | Wound, item used, seconds of being unable to move or shoot |
| Reload | `reload` | Rounds left, magazines available |
| Loot | `loot_c7` | Container, distance |
| Extract | `extract_north` | Route length, time left |
| Hold | `hold` | Keep the current action |

Naming rules:

- A name holds the action kind and a target ID only. Avoid names that carry a judgment, such as `good_cover`. A paper on typed decision models ([arXiv 2609.26758](https://arxiv.org/abs/2609.26758)) reports that changing the mapping between option names and definitions changed decisions sharply while type errors stayed at 0 %.
- The name and description must refer to the same thing.
- The adapter shuffles candidate order with a seed for each decision and writes the order seed to the request. Experiment E3 (section 9.2) measures how much order and naming change choices.

## 3.5 Decision points

The adapter asks only when something happens:

- **Events.** A new threat seen, a threat lost, damage taken, a target down, a wound started, ammo below a quarter, the current tactic ended or failed, a time warning.
- **Timer.** Every 0.5 s of game time during contact (a 2 Hz cap), every 3 s otherwise.
- **Hold-off.** No new question for 0.5 s after a choice, except on damage, so tactics do not flip every step.

Compare against what the last decision saw, not against the previous step, so that changes that happen while an action runs are not missed.

When the decider answers, the adapter does not apply the choice at once. It waits the profile's `decision_delay` in game time (for example 0.2 to 0.35 s) while the executor keeps running the previous action and reflexes. If the choice is no longer legal at that point (target down, cover taken, item gone), the adapter drops it, holds, records `dropped`, and moves the next decision point earlier.

## 3.6 Wire protocol (JSON Lines, version 1)

Game to bridge, one line on stdout, with a prefix that separates it from the game's other output:

```json
@@shakedown {"v":1,"type":"decide","run":"r042","step":5412,"t":90.2,"seq":37,
 "reason":"new_contact","profile":"cautious",
 "state":{"self":{"hp":{"thorax":72,"legs":40},"bleeding":["legs"],"stance":"crouch"},
          "equipment":{"mag":11,"spare_mags":2},
          "seen":[{"id":"e2","bearing":35,"range_m":18,"exposure":"upper_body","since_s":0}],
          "memory":[{"id":"e1","last_seen_s":6,"range_m":31}],
          "cues":[{"kind":"footsteps","bearing":-80}],
          "objective":{"goal":"extract_north","route_m":140,"time_left_s":372},
          "recent":[{"tactic":"withdraw","ended":"failed","seconds":5,"moved_m":0,"seconds_ago":1}]},
 "candidates":{"engage_e2":"Engage e2. 18 m, upper body exposed, 11 rounds",
               "cover_c1":"Move to cover c1. 6 m, 1.2 s exposed, blocks e2",
               "withdraw":"Withdraw south-west. 25 m",
               "hold":"Keep the current action"},
 "order_seed":991}
```

Bridge to game, one line on stdin:

```json
{"v":1,"seq":37,"choice":"cover_c1","source":"jev",
 "probabilities":{"cover_c1":0.71,"engage_e2":0.18,"withdraw":0.09,"hold":0.02}}
```

Other message types:

| `type` | Direction | When | Contents |
|---|---|---|---|
| `hello` | Game → bridge | Once, before the first decision | Protocol version, game name, adapter version, seed, profile |
| `decide` | Game → bridge | At each decision point | As above |
| `end` | Game → bridge | Once, when the session ends | Outcome (`death`, `extract`, `timeout`, `oracle`), step, oracle name if any |

Rules:

- The game flushes stdout right after each line. In the prototype, stdout to a pipe was fully buffered and the two processes deadlocked until a flush was added.
- `source` is one of `random`, `mock`, `tape`, `jev`, `cache`, or `fallback`.
- The request hash covers `state`, the sorted `candidates`, `order_seed`, `profile`, and `step`. It excludes run IDs and wall-clock values, which would make every rerun look like a divergence.
- The hash is the SHA-256 hex digest of the canonical JSON of an object holding exactly those five fields: keys sorted, separators `,` and `:` with no spaces, non-ASCII characters written as UTF-8 rather than `\u` escapes, and NaN or Infinity rejected. An adapter that hashes in its own language (for `tape:` mode) must reproduce these bytes. Float text must match Python's shortest round-trip form, which is one more reason to keep `state` in integers.
- The bridge holds the same contract from its side. A `decide` line that does not parse or validate, a second `hello`, or a `seq` that does not increase makes the bridge stop the game and exit with code 2. A request that does not match the tape when the bridge runs the `tape` decider makes it stop the game and exit with code 5.
- The game exits with code 2 if `seq` does not match, `choice` is not a candidate, or the line does not parse. Contract errors are never skipped silently.

## 3.7 Executor and reflexes

The executor turns one tactic into input events every step. It uses the same input path as a human player, so health, stamina, stance, and aim spread limit the bot exactly as they limit a person. Events go in before the step's input poll, in the same shape the game's own replay system produces, so recorded times do not shift by a step.

Human-like limits come from the profile: reaction delay after first sight, time for the cursor to settle on a target, and seeded aim jitter.

Reflexes bypass the decider. Typical reflexes are re-aiming when the visible target changes, stopping a sprint under fire, and running out of a grenade's blast radius after the profile's reaction time. A reflex never replaces the tactic. It can only move the next decision point earlier.

In the prototype, explosions caused 39.5 % of deaths (423 of 1,071) in the overnight run before a grenade reflex existed. The bots were dying to a hazard they had no way to react to.

## 3.8 Artifacts

| File | Requirement |
|---|---|
| `telemetry.jsonl` | One JSON object per line with `t`, `step`, `type`. At least: `run_begin`, `contact`, `hit`, `damage_taken`, `death`, `extract`, `decision`, `run_end`. `decision` lines hold deterministic fields only (`seq`, step, reason, choice, applied or dropped). Latency and source belong to the tape. |
| `inputs.json` | Every injected event with its exact step time. Write times as the exact float the game computed. A rounded time can land one step off late in a long run. |
| `run_end` | Outcome, step, distance walked, final position, and counts the game cares about |

`inputs.json` is the most useful artifact. A run that found a bug replays from it with no bridge and no model, and a few assertions turn it into a regression test in the game's own test suite.

## 3.9 Exit codes

| Code | Meaning |
|---|---|
| 0 | Normal end. The outcome is in telemetry. |
| 2 | Argument, tuning, or protocol contract error |
| 4 | Stopped by an oracle |
| 5 | Tape divergence |

Code 3 is left to the game, which may already use it (the prototype used 3 for replay assertion failures). A game that already uses these codes for something else can remap them in `shakedown.toml`.

## 3.10 Conformance checks

`shakedown check-adapter` runs these checks against a new adapter. C1 to C3 passed in the prototype (section 1.5). C4 to C6 were its phase acceptance criteria, and C7 was listed there as a test to write.

| Check | Pass condition |
|---|---|
| C1 Repeatable | Two `rule` runs with the same arguments give byte-identical telemetry and `inputs.json` |
| C2 Input replay | Replaying `inputs.json` with no decider gives the same game events (decision lines excluded) |
| C3 Tape replay | A `mock` bridge run replayed with `tape:` gives the same telemetry (`run_begin` excluded) and the same number of decisions |
| C4 Contract errors | A wrong `seq`, an unknown choice, and a broken line each exit with code 2 |
| C5 Divergence | A tampered tape exits with code 5 |
| C6 Oracle | A forced stuck state exits with code 4 and leaves an `inputs.json` that reproduces it |
| C7 No leak | An entity behind a wall never appears in `seen` (game-specific fixture, the agent skill writes a template) |

C1 to C3 all passing is the definition of "this game is supported." When C1 fails, the usual cause is work that finishes on wall-clock time: asynchronous asset loads, budgets measured in milliseconds, background threads. Waiting for a hosted decider gives that work extra wall time it would not get in a fast tape run. The fix is a sync point that completes such work on simulation steps before the next step runs.
