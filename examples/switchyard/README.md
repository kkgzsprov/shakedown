# Switchyard

A deterministic turn-based route puzzle used to exercise Shakedown's thin executor. A courier collects a parcel from the depot and delivers it to a dock. Every action is one ordinary button passed to `Game.tick`; the adapter never writes position or cargo directly. The two maps and profiles remain small enough to inspect.

```sh
python -m examples.switchyard --seed 7 --record-inputs runs/switchyard/inputs.json --telemetry runs/switchyard/telemetry.jsonl
python -m examples.switchyard --replay-inputs runs/switchyard/inputs.json --telemetry runs/switchyard/replay.jsonl
python -m shakedown check-adapter --config examples/switchyard/shakedown.toml --output runs/switchyard-checks
```

Use new output directories. Input replay reconstructs startup state and drives recorded buttons without creating an adapter. It asserts the final outcome and digest of all nondecision events. `--scenario stuck` creates an enclosed fixture whose repeated wait input triggers and reproduces the `stuck` oracle. Maintenance traffic outside visible, unobstructed tiles never enters observations or candidate descriptions. Its hidden identity is never exposed.

`rule`, `bridge`, and `tape:PATH` modes share the same button path. Contract errors exit 2, replay assertion failures 3, oracle stops 4, and tape divergence 5. The adapter asks once per turn, with no wall-clock simulation and no real-time reaction delay. This is a discrete puzzle example, not evidence of real-time aiming, human-like play, or visual quality.
