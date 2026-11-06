**English** | [한국어](ko/04-deciders.md)

# 4. Deciders (System 1)

A decider takes one `decide` request and returns one choice with probabilities. It runs in the bridge process. A game may also have an in-process `rule` decider (section 3.2), which needs no bridge.

## 4.1 Interface

```python
@dataclass(frozen=True)
class Decision:
    choice: str                       # must be a key of request["candidates"]
    probabilities: dict[str, float]   # over the same keys; may be empty for random/mock
    source: str                       # random | mock | tape | jev | cache | fallback
    attempts: int                     # API attempts, 0 for local deciders
    latency_ms: float                 # wall time of the whole call, retries included
    model: str | None                 # exact model ID the provider reported

class Decider(Protocol):
    def decide(self, request: dict, instructions: str) -> Decision: ...
    def close(self) -> None: ...
```

`instructions` is the planner's profile text for this run (section 5.2). It is the only path by which the planner influences play.

Measure `latency_ms` across retries. The prototype found an evaluation script for a hosted decision model that recorded only the last attempt's time, which hides the cost of retries.

## 4.2 Backends

| Backend | Network | Use |
|---|---|---|
| `random` | No | Uniform choice among legal candidates, seeded. The floor every other decider must beat (experiment E1). |
| `mock` | No | Deterministic choice from a hash of the request. For testing the pipeline, not the game. |
| `tape` | No | Answers from a recorded tape, in order. Divergence ends the run. |
| `jev` | Yes | Implemented optional TypeSafe Jev `POST /v1/systemone` choice questions, exact model pin, shared cache and admission ledger. Live provider qualification remains pending. |
| Local open models | No | Planned (section 9.3). Candidates include Laya (public encoder plus decision head) and SemIf (frozen LM label logits). Neither has been tested with Shakedown. |

Jev caches successful answers by request hash, profile instructions, model, and endpoint. Cache hits are counted as `source: "cache"`. An optional SQLite cache persists across invocations; cache and ledger paths are relative to the project configuration file.

## 4.3 Hosted backend example: Jev

The implementation sends this request shape. The model pin below is illustrative, not a claim that this version is currently available:

```json
{
  "model": "jev-1.14.0",
  "state": { "...": "the game's state object, unchanged" },
  "questions": {
    "tactic": {
      "type": "choice",
      "instructions": "<profile text from the planner>",
      "criteria": { "cover_c1": "Move to cover c1. 6 m, ...", "engage_e2": "...", "...": "..." }
    }
  }
}
```

- Pin an exact model ID, never an alias such as `latest`. Record the model ID from each response, and invalidate a campaign whose model changes partway. Mixed versions cannot be compared.
- Choose by `argmax`; tied maxima use sorted candidate names. Seeded hosted sampling is not implemented.
- Read the key from the configured environment variable, `TYPESAFE_API_KEY` by default. Key-file loading is not implemented. The runner removes the hosted key from the game's environment. Never write it to configuration, logs, tapes, or exception text.
- Treat `probabilities` as the model's judgment over the candidate descriptions. It is not the probability that the action succeeds. Confidence definitions also differ between models: Jev documents `(pmax − 1/n) / (1 − 1/n)`, while Laya uses `1 − H(p) / log(n)`. Do not reuse a threshold across deciders.

The prototype measured p50 about 210 ms and p95 about 235 ms over four runs, and $0.0005 to $0.0025 per 60 s run (section 1.5). These are historical prototype measurements, not validation of the current backend. Current local HTTP fixtures establish request/response, timeout, retry, cache, and accounting behavior; live inference, production latency, and actual billing remain unmeasured.

Configure an exact available model and an authorized budget before selecting `jev`. This TOML fragment shows the implemented settings; the model value remains illustrative:

```toml
[decider]
backend = "jev"
model = "jev-1.14.0"
budget_usd = 0.05
call_budget_usd = 0.001
max_calls = 50
timeout_s = 3
retries = 2
backoff_s = 0.1
max_elapsed_s = 12
input_price_per_million = 0.042
api_key_env = "TYPESAFE_API_KEY"
cache = "cache/jev.sqlite3"
ledger = "jev-budget.json"
```

The endpoint defaults to `https://api.typesafe.ai/v1/systemone`. Overrides require HTTPS; plain HTTP is restricted to loopback test fixtures, and redirects are not followed. `cache` and `ledger` are optional. `input_price_per_million` is a configured input-token tariff estimate, not a verified provider charge or a live price lookup. The output-token count is retained separately.

After supplying an account environment and approved settings, choose the hosted path with either command syntax:

```text
shakedown run --config CONFIG --output NEW_BATCH_DIR --deciders jev
shakedown bridge --decider jev --config CONFIG --run-dir NEW_RUN_DIR -- GAME_COMMAND ARGUMENTS
```

Uppercase values are operator-supplied paths and argument arrays, not literal shell commands to copy. The standalone bridge requires `--config` for Jev. A batch shares one runtime's cache, USD reservations, call count, and concurrency limiter across its hosted runs. Planner and gameplay-decider budgets stay separate.

## 4.4 Failure handling and budget

| Situation | Bridge action | Recorded as |
|---|---|---|
| Timeout (default 3 s), 5xx, connection error | Retry at most twice with exponential backoff within `max_elapsed_s`, then fall back | `source: "fallback"`, attempts, whole-call latency |
| HTTP 429 | Halve the shared runtime concurrency cap and retry within the same limits; honor bounded retry delay | Cap change event |
| Other HTTP errors, including 400, 422, and redirects | No automatic retry; stop and invalidate the run without dumping provider bodies or credentials | Sanitized error detail |
| Fallback rate above 10 % in a run | Finish the run, mark it invalid | `bridge_summary.json` |
| Model ID changes during a campaign | Mark the affected runs invalid | `bridge_summary.json` |
| Admission budget or call allowance reached | Start no further hosted attempts; retain the invalid attempt/run evidence | Shared ledger and bridge summary |

The implemented hosted fallback is seeded `random`; game-specific `rule` fallback is not wired into this backend. Aggregation reports invalid runs as `invalid_runs` and leaves them out of decider comparisons. Model mismatch, unknown choices, malformed probabilities, or invalid response envelopes stop the run rather than silently accepting an answer.

Every HTTP attempt reserves `call_budget_usd` before sending. The backend reports token-based `estimated_cost_usd` separately from `cost_usd`, which remains unknown when HTTP occurred. Failed requests or missing usage make the full estimate unknown, and their reservations remain charged; known partial token estimates remain visible. An unused preflight reservation is released on close. The ledger limits admitted work, not the provider's final bill. Bridge/batch receipts retain HTTP attempts, cache hits, model identity, token usage, estimates, fallback/latency data, and cap changes without API keys.

## 4.5 What a decider must not do

- Compute coordinates, aim, or route. The adapter has already done that.
- Judge legality. Every candidate it sees is already legal.
- Invent an action. A choice outside the candidates is a contract error.
- Keep hidden state across requests. Anything the decider should remember goes in `state.recent` or `state.memory`, so that the tape replays it.
