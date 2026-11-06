**English** | [한국어](ko/05-planner-and-skill.md)

# 5. Planner and the agent skill

## 5.1 Planner backends

The M4 planner runs between gameplay runs. It produces structured drafts through a command-line LLM or the offline `mock` backend. M5 adds bounded numeric search and held-out proposal evaluation through `campaign --optimize`; automatic default application remains unimplemented.

| Backend | Call | Notes |
|---|---|---|
| `claude-cli` | `claude -p --output-format json --json-schema <JSON string> --max-budget-usd <n>` | Read the envelope's `structured_output`, not the text `result`. Disable built-in tools, inherited MCP, and customizations for the subprocess. The provider's cost cap is an estimate checked after responses arrive. |
| `codex-cli` | `codex exec --output-schema <file> -o <file> -s read-only --json` | Disabled unless `codex_mode = "restricted"` is explicitly configured. Additional tool and configuration restrictions are applied, but read-only sandboxing is not a tool-free guarantee. Token usage does not establish a dollar charge. |
| `mock` | Fixed JSON derived from the inputs | Runs the whole pipeline with no network. |

Every backend's output is checked against the step's schema and semantic constraints. Invalid schema output permits one retry; a second invalid output stops the step. Each attempt consumes the call and budget allowance. A CLI can also retry internally, so this limit counts Shakedown attempts rather than individual provider requests. The orchestrator pastes input files into the prompt and does not expose Shakedown execution tools to the planner.

Claude's `--tools ""` removes built-in tools; MCP needs a separate empty strict configuration. Codex's restricted mode disables selected capabilities and audits emitted tool-call events, rejecting outputs that used tools. This audit cannot undo an already executed tool call. An empty working directory and ignored user configuration reduce inherited context; they do not certify an operating-system isolation boundary or eliminate managed policy. Never describe the restricted Codex mode as verified tool-free execution.

The CLI contracts were checked against local Codex 0.166.0 and Claude Code 2.1.318 help on 2026-11-06, without a model call. See the official [Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive), [Codex configuration reference](https://developers.openai.com/codex/config-reference), [Claude CLI reference](https://code.claude.com/docs/en/cli-reference), and [Claude structured output documentation](https://code.claude.com/docs/en/headless). Live backend/schema qualification remains a separate manual gate.

## 5.2 Planner steps

| Step | Input | Output (schema) |
|---|---|---|
| `targets` | Game design notes, observed metrics if any | `targets.json` draft: metric, profile, acceptable range. A human edits and approves it. |
| `campaign` | Design notes, tuning key table, approved targets, previous report | `campaign.json`: experiments, hypotheses, instructions per profile, tuning search ranges, batch size |
| `revise` | `metrics.json`, labels, anomalies | `revision.json`: updated hypotheses, next search ranges inside the key table, stop or continue |
| `bug` | Run arguments, oracle line, last telemetry events | `bug_draft.json`: title, reproduction command, expected and actual, suspected cause |
| `report` | Metrics and available supporting evidence | `report.json` plus `report.md` for humans; proposals do not apply defaults |

Profile instructions are the planner's only influence on play. They are prefixed to the decider's question for every decision in a run, limited to 600 characters per profile, and cannot add candidates or new actions. The schema checks length only, so a human reads `campaign.json` before approving it.

Proposed ranges must satisfy the configured key-table constraints before the artifact is accepted. The M5 balance loop also limits revisions to the human-approved campaign's search box, records clipping in `revision.json`, and rejects newly introduced tuning keys. Revisions cannot change profile instructions or the fixed policy.

## 5.3 What the planner does not decide

- **Target ranges.** A human sets them, for example "cautious profile survival 40 to 60 %".
- **The numeric search.** The optimizer does it. This follows RuleSmith's split between LLM and optimizer.
- **Defaults.** Only approved changes reach the game (section 7.4).
- **Fairness verdicts.** "Is this death fair" is answered by comparing human labels with model labels. The planner's opinion is not evidence.

## 5.4 Approval gates

| Gate | Default | Why |
|---|---|---|
| Before a campaign runs | Approved targets and campaign receipts, plus `--approved` at execution | Instructions and ranges steer every run |
| Before planner spending | A budget ledger reserves each call before launch | Failed attempts and unknown charges must not disappear from accounting |
| Before changing defaults | M5 writes reviewable patches; no command applies them | A balance change is a human design decision |

`approve` records an explicit operator assertion bound to artifact content and configuration. Campaign approval also depends on the approved targets. Receipts are unsigned: they detect stale inputs, but do not authenticate the person approving them. Editing an artifact or relevant configuration requires fresh approval. The prototype's direct-default-change workflow is not part of M4.

### M4 commands

```text
shakedown plan {targets,campaign,revise,bug,report} --config PATH --input NAME=PATH --output DIR
  [--backend mock|claude-cli|codex-cli] [--targets PATH --targets-approval PATH]
  [--budget-usd N --call-budget-usd N --max-calls N --ledger PATH]

shakedown approve {targets,campaign} --config PATH --artifact PATH --receipt PATH --approved
  [--targets PATH --targets-approval PATH]

shakedown campaign --config PATH --campaign PATH --approval PATH
  --targets PATH --targets-approval PATH --output DIR --approved [--max-runs N] [--optimize]
```

The wrapped lines describe command syntax, not shell line continuations. Repeat `--input NAME=PATH` for named evidence files. The required name is `design_notes` for `targets` and `campaign`, `metrics` for `revise` and `report`, and `run` for `bug`. The respective output artifacts are `targets.json`, `campaign.json`, `revision.json`, `bug_draft.json`, and both `report.json` and `report.md`. Campaign drafting and approval use the approved targets and their receipt. Without `--optimize`, `campaign` executes one baseline round. With it, the M5 loop uses `[balance]`; `--max-runs` can lower the configured run cap. See chapter 7.

The `[planner]` configuration accepts `backend`, `budget_usd`, `call_budget_usd`, `max_calls`, `timeout_s`, `model`, `command`, `codex_mode`, and `ledger`. `codex_mode` accepts `disabled` or `restricted`. Select the model explicitly for live calls; examples do not pick a provider model or enable model fallback. The plan command's budget and ledger options override the corresponding configured limits for that invocation.

The ledger reserves `call_budget_usd` before each attempt and enforces the total admission budget and maximum call count. Known costs are recorded, and an unknown charge consumes the full reservation. Reservations are not a hard billing cap: a provider can finish a response after reaching its estimated cap, and Codex does not expose a verified USD limit. A failed or interrupted process may have incurred unreported cost. The same accounting applies to the one schema retry. See [Claude budget headroom](https://code.claude.com/docs/en/agent-sdk/agent-loop) and [cost accounting on failures](https://code.claude.com/docs/en/agent-sdk/cost-tracking).

## 5.5 The agent skill

The M6 [`skill/shakedown/`](../skill/shakedown/SKILL.md) package supplies a workflow for coding agents such as Claude Code or Codex. The agent running the skill is the planner. The package gives it:

| Part | Contents |
|---|---|
| `SKILL.md` | When to use Shakedown, the three-layer model, the command list, the approval gates, and what never to do (commit balance changes, put keys in files, judge fairness) |
| Adapter guide | A checklist from chapter 3, ordered as the agent should build it: headless mode → telemetry → input recording and replay → observation → candidates → executor → decision points → oracles |
| Adapter templates | Transport skeletons for C++17, C#, GDScript, and Python, plus a game-owned C7 visibility fixture pattern; game input/replay integration still needs implementation and conformance |
| Scripts | `shakedown check-adapter`, `run`, `explore`, `triage`, `campaign` |

The skill does not ship an executor, because an executor is game-specific code (chapter 3). Writing one is a task the skill hands to the agent, and `check-adapter` is how the agent proves the result works. An adapter counts as done only when checks C1 to C3 pass (section 3.10).

`examples/switchyard/` provides a second discrete-button adapter and exercises the skill verification workflow. Its shipped implementation preceded the skill read. A separate fresh-context trial read the skill, game rules, protocol, and configuration without the shipped adapter and wrote an independent adapter. It passed C1–C6 on seed 7, `depot`, `courier`, meeting the minimum C1–C3 support criterion; C7 remained `unmeasured`. Template compilation, transport behavior, game conformance, and broader blind generalization remain separate claims; inspect the skill's validation notes for each language.

Games with discrete actions per decision, such as turn-based, card, puzzle, or menu-driven games, need almost no executor: each candidate maps to one button or API call. For these the templates include a "thin executor" mode that a table of `candidate → input` covers.

## 5.6 Intended agent session after M6

1. The user asks the agent to playtest their game.
2. The agent reads the game, writes or updates the adapter, and runs `shakedown check-adapter` until C1 to C3 pass.
3. The agent drafts `targets.json` and `campaign.json` and asks the user to approve both.
4. The agent runs `shakedown explore` (QA) or `shakedown campaign` (balance) within the approved budget.
5. The agent reads `findings/README.md`, `anomalies.md`, and `report.md`, and shows the user the reproduced bugs and proposed changes, with input recordings that replay each one.
