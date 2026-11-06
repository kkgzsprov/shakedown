# Adapter implementation and acceptance

Implement the game boundary in this order, verifying each stage against the actual executable:

1. A seeded headless session on a fixed simulation step. No asynchronous work may complete according to wall-clock time. Copy mutable startup saves/profiles into each isolated run directory.
2. JSONL telemetry and input recording. Every event has `type`, nonnegative `step`, and simulation `t`. Keep one `run_begin` and one `run_end`. Record every injected input before the step's ordinary input poll, with the exact time computed by the game.
3. Input-only replay. Load the complete startup configuration and recorded input stream; do not construct a decider or executor. Reject missing/duplicate steps and verify the final result and deterministic game-event digest. A successfully reproduced oracle retains its oracle exit code.
4. Player-visible observations, legal candidate descriptions, and the game-owned executor. Compute distances, visibility and legality in the adapter. Exclude enemies behind walls and other privileged world state. Use a profile's reaction limits for both ordinary tactics and reflexes.
5. Decision points and stdio transport. Pause simulation while waiting for the decision. Continue the old tactic during the profile's simulation-time decision delay; recheck legality on application and record `applied` or `dropped` with the same `seq`.
6. Contract, tape-divergence, forced-oracle and no-leak fixtures. Keep every verdict and its evidence path.

## Version 1 transport

The game writes exactly one UTF-8 JSON object per line, prefixed with `@@shakedown `, and flushes after each line. Ordinary log lines must not use that prefix. The bridge replies with an unprefixed JSON line on stdin. Never advance game time while blocking for stdin.

Send one `hello` before any decision:

```json
{"v":1,"type":"hello","game":"my-game","adapter_version":"1","seed":7,"profile":"cautious"}
```

A `decide` request requires `v:1`, `type:"decide"`, strictly increasing integer `seq`, nonnegative integer `step`, `reason`, `profile`, object `state`, object `candidates`, and integer `order_seed`. Supply 1–12 legal named candidates with factual string descriptions. `run` and finite simulation `t` are optional wire fields. Seed the candidate permutation and record its `order_seed`.

```json
{"v":1,"type":"decide","seq":1,"step":12,"reason":"timer","profile":"cautious","state":{"self":{"hp":100},"seen":[]},"candidates":{"hold":"Keep the current action"},"order_seed":17}
```

The response must be valid newline-terminated JSON with `v:1`, the exact integer `seq`, and a `choice` key present in that request's candidates. `source` is `random`, `mock`, `tape`, `jev`, `cache`, or `fallback`; `probabilities` are provider judgments, not action success probabilities. Reject malformed JSON, mismatched sequences, or invented choices immediately with exit code 2. Do not retry or silently select another action on a contract error.

Send one `end` with `outcome` (`death`, `extract`, `timeout`, or `oracle`) and integer `step`; include an oracle name for an oracle stop. Normal exit is 0, contract error 2, oracle stop 4, tape divergence 5. Exit 3 is available to the game, for example for an input-replay assertion failure. Configure remapped game codes explicitly.

## Hashing and replay

The request digest is SHA-256 of UTF-8 canonical JSON containing exactly `state`, `candidates`, `order_seed`, `profile`, and `step`. Sort object keys at every depth, use separators `,` and `:` without spaces, preserve literal Unicode, and reject NaN/Infinity. Exclude run IDs, `seq`, reasons and wall-clock timing. Integer or rounded state avoids cross-language float-format differences. Python's shortest round-trip float spelling is the reference.

Use one golden hash fixture across languages before declaring tape support. A `tape:<file>` adapter must compare each newly generated request hash with the next recorded decision. A mismatch, exhausted tape, or recorded illegal choice exits 5. Input replay is a different path: it uses recorded inputs and constructs no adapter.

Keep backend source, latency and cost out of deterministic game telemetry. Decision receipts hold `seq`, deterministic reason/choice and selected/applied/dropped state. Record all injected inputs, full startup configuration and final assertions in `inputs.json`; compare replayed game events after excluding decision lines.

## Configuration and real-process checks

Supply a `shakedown.toml` with shell-free game argument arrays, maps, profiles, seeds and offline deciders. Command placeholders include `{python}`, `{project_dir}`, `{seed}`, `{map}`, `{profile}`, `{seconds}`, `{run_dir}`, `{decider_mode}`, `{tuning_file}`, and `{inputs_file}`. Use `game.replay_command` for input-only replay. Add immutable startup files to `game.fixtures`; paths stay inside the game project.

Use [the complete configuration guide and example](configuration.md) for exact TOML sections, path resolution, all supported placeholders and the `tuning_version`/`values` JSON envelope. Save TOML as UTF-8 without a BOM, including on Windows.

Run `check-adapter --config CONFIG --output NEW_DIR` and inspect `conformance.json`:

| Check | Evidence required |
|---|---|
| C1 | Two same-seed `rule` runs give byte-identical telemetry and input recording. |
| C2 | Input replay reproduces game events, excluding decisions. |
| C3 | `mock` bridge recording replayed by `tape:` reproduces telemetry, excluding `run_begin`, with matching decision count. |
| C4 | Wrong sequence, unknown choice and malformed JSON each immediately exit 2. |
| C5 | A tampered tape exits 5. |
| C6 | Configured forced-oracle arguments produce the named oracle and input-only replay reproduces it. |
| C7 | The game-owned visibility fixture exits 0 and emits JSON with `passed:true`; hidden entities stay absent and a visible positive control appears. |

C1–C3 passing means the game is supported. Overall conformance passes only with all seven checks passed. Missing `game.replay_command`, oracle arguments or visibility fixtures remain unmeasured. Configure `checks.oracle_args`, `checks.oracle_seconds`, `checks.oracle_kind`, and `checks.visibility_command` for the game; do not use a vacuous fixture returning success.
