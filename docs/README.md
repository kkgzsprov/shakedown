**English** | [한국어](ko/README.md)

# Shakedown design documents

Status: M0 and M1 are implemented; M2 and later remain design. These documents describe what is and will be built. Chapter 8 covers the runnable tinyraid example; the roadmap (chapter 9) says what comes next.

| Chapter | Read it for |
|---|---|
| [1. Overview](01-overview.md) | What Shakedown does, the three layers, why the game pauses, evidence so far, related work |
| [2. Architecture](02-architecture.md) | Processes, components, repository layout, `shakedown.toml`, run directory |
| [3. Adapter and wire protocol](03-adapter-protocol.md) | What a game must implement, the JSON Lines protocol, exit codes, conformance checks |
| [4. Deciders](04-deciders.md) | The System 1 interface, backends, failure handling, budgets |
| [5. Planner and agent skill](05-planner-and-skill.md) | The System 2 steps, approval gates, the skill for coding agents |
| [6. Oracles, exploration, and triage](06-oracles-and-triage.md) | How bugs are flagged, found, grouped, and reproduced |
| [7. Balance loop](07-balance-loop.md) | Tuning keys, search, holdout seeds, the proposal gate |
| [8. Example game: tinyraid](08-example-game.md) | The bundled game, its adapter, seeded bugs, the benchmark |
| [9. Roadmap](09-roadmap.md) | Milestones, release experiments, open questions |

## Terms

| Term | Meaning |
|---|---|
| Run | One game session from start to death, extraction, or time limit |
| Batch | A set of runs over seeds, profiles, maps, and tuning candidates |
| Campaign | Several batches toward approved targets, with planner revisions between them |
| Planner | System 2: an LLM that plans and reports between batches |
| Decider | System 1: chooses one legal candidate at each decision point |
| Executor | Game-side code that turns a choice into input every step |
| Adapter | All game-side Shakedown code: observation, candidates, executor, reflexes, oracles, pause |
| Candidate | A legal action of a few seconds, named by kind and target |
| Decision point | A moment when the adapter pauses and asks the decider |
| Tape | The recorded answers of one run, used to replay it without the decider's backend |
| Input recording | Every input event the executor injected, used to replay a run with no decider |
| Profile | A player temperament: planner instructions plus executor limits such as reaction delay |

## Translations

Korean translations live in [`ko/`](ko/README.md) with the same file names. The English documents are the source. When an English document changes, its translation is updated in the same change, or marked as out of date at the top.
