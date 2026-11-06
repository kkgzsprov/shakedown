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
| `jev` | Yes | TypeSafe Jev `POST /v1/systemone` with a `choice` question. Optional. |
| Local open models | No | Planned (section 9.3). Candidates include Laya (public encoder plus decision head) and SemIf (frozen LM label logits). Neither has been tested with Shakedown. |

A cache wraps any network backend. Requests with the same hash reuse the answer and are counted as `source: "cache"`.

## 4.3 Hosted backend example: Jev

The prototype's request:

```json
{
  "model": "jev-1.14.0",
  "state": { "...": "the game's state object, unchanged" },
  "questions": {
    "tactic": {
      "type": "choice",
      "instructions": "<profile text from the planner> Choose what this player does for the next few seconds.",
      "criteria": { "cover_c1": "Move to cover c1. 6 m, ...", "engage_e2": "...", "...": "..." }
    }
  }
}
```

- Pin an exact model ID, never an alias such as `latest`. Record the model ID from each response, and invalidate a campaign whose model changes partway. Mixed versions cannot be compared.
- Choose by `argmax` by default. Seeded sampling at temperature 1 is allowed for exploration, and the tape records which rule was used.
- Read keys from an environment variable (`TYPESAFE_API_KEY`) or a key file outside the repository. Never write a key to logs, tapes, or exception text.
- Treat `probabilities` as the model's judgment over the candidate descriptions. It is not the probability that the action succeeds. Confidence definitions also differ between models: Jev documents `(pmax − 1/n) / (1 − 1/n)`, while Laya uses `1 − H(p) / log(n)`. Do not reuse a threshold across deciders.

The prototype measured p50 about 210 ms and p95 about 235 ms over four runs, and $0.0005 to $0.0025 per 60 s run (section 1.5). Rate limits were not measured.

## 4.4 Failure handling and budget

| Situation | Bridge action | Recorded as |
|---|---|---|
| Timeout (default 3 s), 5xx, connection error | Retry twice with exponential backoff, then fall back | `source: "fallback"`, attempts |
| 429 or a rate-limit response | Halve the concurrency cap and retry | Cap change event |
| 400 or 422 | No retry. Dump the request and stop the run. | Error detail, truncated |
| Fallback rate above 10 % in a run | Finish the run, mark it invalid | `bridge_summary.json` |
| Model ID changes during a campaign | Mark the affected runs invalid | `bridge_summary.json` |
| Campaign budget reached | Start no new runs | Cost total |

The fallback decider is `random` unless the game provides `rule`. Aggregation reports invalid runs as `invalid_runs` and leaves them out of decider comparisons.

## 4.5 What a decider must not do

- Compute coordinates, aim, or route. The adapter has already done that.
- Judge legality. Every candidate it sees is already legal.
- Invent an action. A choice outside the candidates is a contract error.
- Keep hidden state across requests. Anything the decider should remember goes in `state.recent` or `state.memory`, so that the tape replays it.
