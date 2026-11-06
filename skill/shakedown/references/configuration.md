# A complete run configuration

[The complete `shakedown.toml` example](../assets/shakedown.toml) runs the repository's Switchyard game with no network. It includes normal execution, input-only replay, two offline policies, tuning and all game-owned conformance fixtures. From the Shakedown checkout:

```text
python skill/shakedown/scripts/run_shakedown.py check-adapter --config skill/shakedown/assets/shakedown.toml --output runs/switchyard-check
```

Choose a new output directory. When adapting another game, copy the file and edit the game's arguments, maps, profiles, tuning table and fixture commands. Do not retain Switchyard's fixture commands for a different game. `game.cwd` and `tuning.keys` are resolved relative to the TOML file's directory; argument placeholders provide absolute execution paths where needed. The example's `../../..` paths assume its checked-in location, so change them after copying.

Save TOML as **UTF-8 without a BOM**. Python's `tomllib` rejects a leading BOM. On Windows, `Path.write_text(text, encoding="utf-8")` writes the intended encoding; older PowerShell's `Set-Content -Encoding utf8` may add a BOM.

## Argument arrays and paths

`game.command` and `game.replay_command` are arrays of literal arguments. They do not run through a shell. Keep each path as one element; do not add shell quoting around a placeholder or use shell redirection. Only the following placeholders are supported, with no format specifiers:

| Placeholder | Supplied value |
|---|---|
| `{python}` | The current Python interpreter. |
| `{project_dir}` | Absolute `game.cwd`. Use it for source-checkout asset paths. |
| `{exe}` | `game.exe`, when explicitly configured. |
| `{seed}`, `{map}`, `{profile}` | The run's selected dimensions. |
| `{seconds}` | Fixed simulation duration. |
| `{run_dir}` | Absolute directory for that isolated run's evidence. |
| `{decider_mode}` | `rule`, `bridge`, or `tape:<absolute file>`. |
| `{tuning_file}` | Generated numeric-tuning JSON file for the run. |
| `{inputs_file}` | Recorded input file supplied to `game.replay_command`. |

The orchestrator runs the executable from an isolated working directory, not `game.cwd`. Use `{project_dir}` for relative game assets. The Python example uses the source checkout on `PYTHONPATH`; a native game can instead set `game.exe` and start `command = ["{exe}", ...]`. Flag names are game-owned; adapt them to the actual executable.

## Tuning values are wrapped

The file at `{tuning_file}` is an envelope, not a bare key/value map:

```json
{"tuning_version":1,"values":{}}
```

`values` contains only numeric overrides, for example `{"player.speed":5.0}` for a game exposing that key. `{}` preserves the game's documented defaults. Validate the envelope version, known keys, numeric types and allowed ranges in the adapter before a run starts. `tuning.keys` names the game's separate schema table containing each key's `type`, `min`, `max`, and documented `default`; the game must actually use those values.

`tuning.candidates = [{}]` measures the baseline. Search settings for `campaign --optimize` belong in `[balance]` and must be approved together with the campaign; this offline conformance example intentionally does not enable optimization or hosted calls.
