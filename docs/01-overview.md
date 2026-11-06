**English** | [한국어](ko/01-overview.md)

# 1. Overview

Status: milestone M0 (protocol, bridge, offline deciders) runs and is tested against a fake game. Everything else below describes what will be built, and section 5 lists which parts already exist as a prototype elsewhere.

## 1.1 What Shakedown does

Shakedown runs bot players through a game before human players do. It looks for two kinds of problems, and it keeps them apart:

1. **Bugs.** A player stuck in geometry, a teleport, a negative ammo count, a crash, a door that opens without its key. Every finding comes with a seed, a decision tape, and an input recording that replays the bug without any model.
2. **Balance.** Distributions of outcomes (survival rate, time to first contact, resource use) across player profiles and tuning values. Shakedown produces evidence for a human to decide on. It never changes defaults on its own.

## 1.2 Three layers

A bot player in Shakedown is three pieces of software, each running at its own rate.

| Layer | Who | When | Produces | Never does |
|---|---|---|---|---|
| Planner (System 2) | An LLM: Claude Code, Codex CLI, or a mock | Before a campaign and between batches of runs | Test plan, hypotheses, profile instructions, tuning ranges, reports, bug drafts | Run during play, emit input, change game defaults |
| Decider (System 1) | A decision model, a random baseline, a game rule, or a tape | At decision points during a run (events, plus a time cap such as 2 Hz of game time) | One choice among legal candidates, with probabilities | Compute coordinates, aim, judge legality |
| Executor | Game-side code in the adapter | Every simulation step | Keyboard, mouse, or controller events through the same input path a player uses | Choose tactics |

The planner sets goals and temperament. The decider chooses within the options the game offers. The executor carries out the choice frame by frame. Code-level oracles watch every step, and a human approves goals and any change to the game.

## 1.3 Why the game pauses for the decider

At each decision point the game stops its simulation clock, writes one request line, and waits for one answer line. This lockstep design has two consequences:

- **Latency stops mattering.** A hosted decision model answers in a few hundred milliseconds. In lockstep that wait costs wall-clock time and zero game time, so a slow model plays exactly as well as a fast one. Human reaction time is modeled separately as a profile value in the executor (for example a 0.2 to 0.35 s decision delay).
- **Runs become replayable.** Each answer is written to a decision tape. Feeding the tape back replays the run without calling the model, and the executor's own input recording replays it without the decider at all. A bug found once can be found again on every build.

The cost is that Shakedown needs code inside the game: something must pause the clock, build candidates, and turn a choice into input. That code is the adapter (chapter 3). Shakedown cannot test a game it cannot pause.

## 1.4 What it is not

- **A screen-reading agent.** Shakedown does not drive a game through screenshots and OS input. That approach cannot pause game time, so it cannot replay what it found.
- **An enemy AI.** The bots play the player's side. Putting a decision model inside enemy AI is a separate problem.
- **A judge of fun.** Shakedown reports distributions. Whether a game feels fair is decided by comparing those distributions with human play.
- **Tied to one model provider.** Deciders and planners sit behind interfaces (chapters 4 and 5). Rule, random, mock, and tape deciders run with no network and no key.

## 1.5 Evidence so far

The design was first built inside the author's unreleased top-down extraction shooter (C++ game, Python tooling, October–November 2026). That prototype is not part of this repository. The numbers below come from that one game, run by the author on one machine, and nobody else has reproduced them.

- **Determinism.** Two runs with the same arguments produced byte-identical telemetry and input recordings (one map, seed 11, 90 s). Replaying the input recording reproduced every game event. Replaying a mock decision tape reproduced the telemetry (seed 31, 18 decisions).
- **Hosted decider.** Four 60 s runs with TypeSafe Jev 1.14 as the decider: no fallbacks, latency p50 about 210 ms and p95 about 235 ms, cost $0.0005 to $0.0025 per run.
- **Instruction sensitivity.** In the first two Jev runs, an aggressive profile with full health and ammo chose to extract in all 20 decisions, because the state did not say how much time was left. After adding `explore_seconds_left` and `raid_seconds_left` to the state, the same seed gave 0 extract choices out of 11. The decider follows what the state tells it, so state design matters as much as model choice.
- **Overnight run.** 1,563 runs in 4.7 hours with 8 workers, $2.28 of Jev calls. No crashes, no contract errors, no tape divergence. Two stuck-player bugs were found, and both reproduced on re-run. Both came from the bot re-choosing the same failed tactic every 0.5 s, which led to adding recent tactic outcomes to the state (section 3.3).
- **What was not shown.** Whether Jev beats a hand-written rule as a decider. Whether the bots' outcome distributions resemble human play. Whether any balance change the loop proposed was an improvement. Section 9.2 lists these as release experiments.

## 1.6 Related work

No public project found so far combines a planner LLM, a separate decision model, and a game-side executor with tape replay for QA. Several projects cover parts of it:

- [playtest-agent](https://github.com/kevinnie2003/playtest-agent) (MIT) runs a scripted explorer with generic oracles and calls an LLM only when stuck, for check-ins, and for triage. It reproduces findings from recorded action sequences. Its README credits personas, not the LLM, for raising seeded-bug recall from 0.38 to 1.0.
- [MIMIC-Py](https://mimic-persona.github.io/MIMIC-Py-Home-Page) (FSE 2026 tool demo) uses an LLM planner conditioned on player personality types, an action executor, a summarizer, and memory. It has no separate fast decision layer.
- [TITAN](https://arxiv.org/abs/2509.22170) tests an MMORPG with an LLM agent and LLM oracles, and reports deployment in eight QA pipelines (author-reported).
- [RuleSmith](https://arxiv.org/abs/2602.06232) tunes game rules with LLM self-play and Bayesian optimization. Shakedown's balance loop (chapter 7) borrows its split between the LLM and the optimizer.
- The Hierarchical Language Agent ([AAMAS 2024](https://www.ifaamas.org/Proceedings/aamas2024/pdfs/p1219.pdf)) uses a large LLM, a small LLM, and a scripted executor for real-time cooperation in Overcooked. The layering is close to Shakedown's, but the goal is cooperation, not testing.

These summaries come from READMEs, abstracts, and project pages. None of these systems has been run for this document.
