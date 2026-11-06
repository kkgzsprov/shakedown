**English** | [한국어](ko/05-planner-and-skill.md)

# 5. Planner (System 2) and the agent skill

## 5.1 Planner backends

The planner is an LLM called through a command-line tool, never from inside a run.

| Backend | Call | Notes |
|---|---|---|
| `claude-cli` | `claude -p --output-format json --json-schema <file> --max-budget-usd <n>` | One call costs about $0.4 in system-prompt cache creation alone (prototype measurement, 2026-10-29). Set the budget with that in mind. |
| `codex-cli` | `codex exec --output-schema <file> -o <file> -s read-only` | Strict schema mode rejects free-form key maps. Shakedown sends maps as `[{key, value}]` arrays to Codex and converts the answer back. |
| `mock` | Fixed JSON derived from the inputs | Runs the whole pipeline with no network. |

Every backend's output is checked against the same JSON Schema. On failure the planner is asked once more. On a second failure the step stops. The planner gets no tool permissions: the orchestrator pastes input files into the prompt.

## 5.2 Planner steps

| Step | Input | Output (schema) |
|---|---|---|
| `targets` | Game design notes, observed metrics if any | `targets.json` draft: metric, profile, acceptable range. A human edits and approves it. |
| `campaign` | Design notes, tuning key table, approved targets, previous report | `campaign.json`: experiments, hypotheses, instructions per profile, tuning search ranges, batch size |
| `revise` | `metrics.json`, labels, anomalies | `revision.json`: updated hypotheses, next search ranges inside the key table, stop or continue |
| `bug` | Run arguments, oracle line, last telemetry events | `bug_draft.json`: title, reproduction command, expected and actual, suspected cause |
| `report` | All metrics, revisions, holdout results | `report.md` for humans, with proposed default changes in a separate table |

Profile instructions are the planner's only influence on play. They are prefixed to the decider's question for every decision in a run, limited to 600 characters per profile, and cannot add candidates or new actions. The schema checks length only, so a human reads `campaign.json` before approving it.

Ranges the planner proposes outside the key table are clipped by the orchestrator, and the clipping is recorded in `revision.json`.

## 5.3 What the planner does not decide

- **Target ranges.** A human sets them, for example "cautious profile survival 40 to 60 %".
- **The numeric search.** The optimizer does it. This follows RuleSmith's split between LLM and optimizer.
- **Defaults.** Only approved changes reach the game (section 7.4).
- **Fairness verdicts.** "Is this death fair" is answered by comparing human labels with model labels. The planner's opinion is not evidence.

## 5.4 Approval gates

| Gate | Default | Why |
|---|---|---|
| Before a campaign runs | `campaign.json` must be approved (`--approved`) | Instructions and ranges steer every run |
| Before spending | Separate budgets for decider and planner (`budget_usd`) | Both bill per call |
| Before changing the game | Shakedown writes a patch and a report. It never commits. | A balance change is a design decision |

The prototype let one owner-approved gate commit tuned defaults directly after a build and test pass. Shakedown keeps that as an opt-in for single-owner projects, not as the default.

## 5.5 The agent skill

`skill/shakedown/` packages Shakedown for coding agents such as Claude Code or Codex. The agent running the skill is the planner. The skill gives it:

| Part | Contents |
|---|---|
| `SKILL.md` | When to use Shakedown, the three-layer model, the command list, the approval gates, and what never to do (commit balance changes, put keys in files, judge fairness) |
| Adapter guide | A checklist from chapter 3, ordered as the agent should build it: headless mode → telemetry → input recording and replay → observation → candidates → executor → decision points → oracles |
| Adapter templates | Skeleton code for the protocol loop in C++, C#, GDScript, and Python, plus the C7 no-leak test template |
| Scripts | `shakedown check-adapter`, `run`, `explore`, `triage`, `campaign` |

The skill does not ship an executor, because an executor is game-specific code (chapter 3). Writing one is a task the skill hands to the agent, and `check-adapter` is how the agent proves the result works. An adapter counts as done only when checks C1 to C3 pass (section 3.10).

Games with discrete actions per decision, such as turn-based, card, puzzle, or menu-driven games, need almost no executor: each candidate maps to one button or API call. For these the templates include a "thin executor" mode that a table of `candidate → input` covers.

## 5.6 Typical agent session

1. The user asks the agent to playtest their game.
2. The agent reads the game, writes or updates the adapter, and runs `shakedown check-adapter` until C1 to C3 pass.
3. The agent drafts `targets.json` and `campaign.json` and asks the user to approve both.
4. The agent runs `shakedown explore` (QA) or `shakedown campaign` (balance) within the approved budget.
5. The agent reads `findings/README.md`, `anomalies.md`, and `report.md`, and shows the user the reproduced bugs and proposed changes, with input recordings that replay each one.
