"""Validated project settings and shell-free game command templates."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path, PureWindowsPath
import string
import sys
import tomllib


BACKENDS = frozenset({"rule", "mock", "random"})
FIELDS = frozenset({"python", "project_dir", "exe", "seed", "map", "profile",
                    "seconds", "run_dir", "decider_mode", "tuning_file", "inputs_file"})
EXIT_CODES = {"ok": 0, "contract": 2, "oracle": 4, "divergence": 5}


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    root: Path
    command: list[str]
    replay_command: list[str] | None
    maps: tuple[str, ...]
    profiles: tuple[str, ...]
    seeds: tuple[int, ...]
    backends: tuple[str, ...]
    duration: float
    workers: int
    wall_timeout: float
    bootstrap_samples: int
    bootstrap_seed: int
    tuning_candidates: tuple[dict, ...]
    instructions: str
    fixtures: tuple[str, ...]
    exit_codes: dict[str, int]
    checks: dict
    raw: dict


def _table(value, name: str, allowed: set[str]) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{name} must be a table")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {name} keys: {', '.join(sorted(unknown))}")
    return value


def _number(value, name: str, *, minimum: float = 0, strict: bool = False) -> float:
    try:
        valid = (type(value) in (int, float) and math.isfinite(value)
                 and (value > minimum if strict else value >= minimum))
        if valid:
            return float(value)
    except (OverflowError, ValueError):
        pass
    raise ValueError(f"{name} must be finite and {'greater than' if strict else 'at least'} {minimum}")


def _integer(value, name: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"invalid integer {name}")
    return value


def _names(value, name: str) -> tuple[str, ...]:
    if (type(value) is not list or not value
            or any(type(item) is not str or not item or "\x00" in item for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{name} must be a nonempty array of unique strings")
    return tuple(value)


def _argv(value, name: str, *, empty: bool = False) -> list[str]:
    if (type(value) is not list or (not value and not empty)
            or any(type(item) is not str or "\x00" in item for item in value)
            or (value and not value[0])):
        raise ValueError(f"{name} must be an argument array")
    return list(value)


def _parts(argument: str):
    try:
        parts = list(string.Formatter().parse(argument))
    except ValueError as error:
        raise ValueError("malformed command template braces") from error
    for _, field, format_spec, conversion in parts:
        if field is not None and (field not in FIELDS or format_spec or conversion):
            raise ValueError(f"unsupported command placeholder: {field}")
    return parts


def _template(value, name: str, *, exe: str | None) -> list[str]:
    argv = _argv(value, name)
    for argument in argv:
        for _, field, _, _ in _parts(argument):
            if field == "exe" and exe is None:
                raise ValueError("{exe} requires game.exe")
    return argv


def _file(base: Path, value, name: str) -> Path:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"{name} must be a file path")
    path = (base / value).resolve()
    if not path.is_file():
        raise ValueError(f"{name} file does not exist: {path}")
    return path


def _fixtures(root: Path, value) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError("game.fixtures must be an array")
    result = []
    for name in value:
        if type(name) is not str or not name or "\x00" in name:
            raise ValueError("invalid fixture path")
        portable = name.replace("\\", "/")
        relative = Path(portable)
        if relative.is_absolute() or PureWindowsPath(name).drive or ".." in relative.parts:
            raise ValueError("fixtures must stay within the project root")
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"fixture must be a file inside the project root: {name}")
        normalized = relative.as_posix()
        if normalized in result:
            raise ValueError("duplicate fixture path")
        result.append(normalized)
    return tuple(result)


def _tunings(settings: dict, config_dir: Path) -> tuple[dict, ...]:
    candidates = settings.get("candidates", [{}])
    if type(candidates) is not list or not candidates or any(type(item) is not dict for item in candidates):
        raise ValueError("tuning.candidates must be a nonempty array of tables")
    keys = None
    if "keys" in settings:
        path = _file(config_dir, settings["keys"], "tuning.keys")
        data = json.loads(path.read_text(encoding="utf-8"))
        if type(data) is not dict or type(data.get("keys")) is not dict:
            raise ValueError("tuning key table must contain a keys object")
        keys = data["keys"]
        for key, spec in keys.items():
            if (type(key) is not str or type(spec) is not dict
                    or type(spec.get("type")) is not str or spec["type"] not in {"int", "float"}):
                raise ValueError("invalid tuning key specification")
            low = _number(spec.get("min"), f"{key}.min", minimum=-math.inf)
            high = _number(spec.get("max"), f"{key}.max", minimum=-math.inf)
            if low > high:
                raise ValueError(f"reversed tuning bounds: {key}")
            if spec["type"] == "int" and any(type(spec.get(bound)) is not int for bound in ("min", "max")):
                raise ValueError(f"integer tuning bounds required: {key}")
            if "default" in spec:
                _tuning_value(key, spec["default"], spec)
    if keys is None and any(candidates):
        raise ValueError("nonempty tuning candidates require tuning.keys")
    for candidate in candidates:
        for key, value in candidate.items():
            if key not in keys:
                raise ValueError(f"unknown tuning key: {key}")
            _tuning_value(key, value, keys[key])
    # Equal candidates would duplicate seed observations and make pairing ambiguous.
    encoded = [json.dumps(item, sort_keys=True, allow_nan=False) for item in candidates]
    if len(set(encoded)) != len(encoded):
        raise ValueError("duplicate tuning candidates")
    return tuple(dict(item) for item in candidates)


def _tuning_value(key: str, value, spec: dict) -> None:
    numeric = _number(value, f"tuning value {key}", minimum=-math.inf)
    if spec["type"] == "int" and type(value) is not int:
        raise ValueError(f"integer tuning value required: {key}")
    checked = value if spec["type"] == "int" else numeric
    if not spec["min"] <= checked <= spec["max"]:
        raise ValueError(f"out-of-range tuning value: {key}")


def load_config(path: str | Path) -> ProjectConfig:
    path = Path(path).resolve()
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    _table(raw, "configuration", {"game", "batch", "decider", "tuning", "checks", "planner"})
    game = _table(raw.get("game", {}), "game", {
        "command", "replay_command", "exe", "cwd", "maps", "profiles", "fixtures", "exit_codes"})
    batch = _table(raw.get("batch", {}), "batch", {
        "seeds", "deciders", "seconds", "workers", "wall_timeout_s", "bootstrap_samples", "bootstrap_seed"})
    decider = _table(raw.get("decider", {}), "decider", {"backend", "budget_usd", "instructions"})
    tuning = _table(raw.get("tuning", {}), "tuning", {"keys", "candidates"})
    checks = _table(raw.get("checks", {}), "checks", {
        "oracle_args", "oracle_seconds", "oracle_kind", "visibility_command"})
    planner = _table(raw.get("planner", {}), "planner", {"backend", "budget_usd"})
    cwd = game.get("cwd", ".")
    if type(cwd) is not str or not cwd or "\x00" in cwd:
        raise ValueError("game.cwd must be a directory path")
    root = (path.parent / cwd).resolve()
    if not root.is_dir():
        raise ValueError("game.cwd must exist")
    exe = game.get("exe")
    if exe is not None and (type(exe) is not str or not exe or "\x00" in exe or "{" in exe or "}" in exe):
        raise ValueError("game.exe must be an executable path or name")
    command = _template(game.get("command"), "game.command", exe=exe)
    replay = (_template(game["replay_command"], "game.replay_command", exe=exe)
              if "replay_command" in game else None)
    maps = _names(game.get("maps", ["default"]), "game.maps")
    profiles = _names(game.get("profiles", ["default"]), "game.profiles")
    seeds = batch.get("seeds", [0])
    if type(seeds) is not list or not seeds:
        raise ValueError("batch.seeds must be a nonempty integer array")
    seeds = tuple(_integer(seed, "seed") for seed in seeds)
    if len(set(seeds)) != len(seeds):
        raise ValueError("duplicate batch seeds")
    backend = decider.get("backend", "mock")
    if type(backend) is not str or backend not in BACKENDS:
        raise ValueError("M2 decider.backend must be rule, mock, or random")
    backends = _names(batch.get("deciders", [backend]), "batch.deciders")
    if any(item not in BACKENDS for item in backends):
        raise ValueError("M2 batch deciders must be rule, mock, or random")
    _number(decider.get("budget_usd", 0), "decider.budget_usd")
    _number(planner.get("budget_usd", 0), "planner.budget_usd")
    if "backend" in planner and (type(planner["backend"]) is not str
                                  or planner["backend"] not in {"mock", "claude-cli", "codex-cli"}):
        raise ValueError("unknown planner backend (planner execution is not implemented in M2)")
    instructions = (_file(path.parent, decider["instructions"], "decider.instructions").read_text(encoding="utf-8")
                    if "instructions" in decider else "")
    exit_codes = dict(EXIT_CODES)
    overrides = _table(game.get("exit_codes", {}), "game.exit_codes", set(EXIT_CODES))
    exit_codes.update({name: _integer(value, f"exit code {name}", 0) for name, value in overrides.items()})
    if len(set(exit_codes.values())) != len(exit_codes):
        raise ValueError("game exit codes must be distinct")
    checks = dict(checks)
    if "oracle_kind" in checks and (type(checks["oracle_kind"]) is not str or not checks["oracle_kind"]):
        raise ValueError("checks.oracle_kind must be a nonempty stuck-oracle identifier")
    if "oracle_args" in checks:
        checks["oracle_args"] = _argv(checks["oracle_args"], "checks.oracle_args", empty=True)
    if "oracle_seconds" in checks:
        checks["oracle_seconds"] = _number(checks["oracle_seconds"], "checks.oracle_seconds", strict=True)
    if "visibility_command" in checks:
        checks["visibility_command"] = _template(checks["visibility_command"], "checks.visibility_command", exe=exe)
    return ProjectConfig(
        path=path, root=root, command=command, replay_command=replay, maps=maps, profiles=profiles,
        seeds=seeds, backends=backends,
        duration=_number(batch.get("seconds", 30), "batch.seconds", strict=True),
        workers=_integer(batch.get("workers", 1), "batch.workers", 1),
        wall_timeout=_number(batch.get("wall_timeout_s", 30), "batch.wall_timeout_s", strict=True),
        bootstrap_samples=_integer(batch.get("bootstrap_samples", 1000), "batch.bootstrap_samples", 1),
        bootstrap_seed=_integer(batch.get("bootstrap_seed", 0), "batch.bootstrap_seed"),
        tuning_candidates=_tunings(tuning, path.parent), instructions=instructions,
        fixtures=_fixtures(root, game.get("fixtures", [])), exit_codes=exit_codes, checks=checks, raw=raw,
    )


def expand_command(config: ProjectConfig, template: list[str], *, run_dir: Path,
                   seed: int, map_name: str, profile: str, decider_mode: str,
                   duration: float, tuning_file: Path, inputs_file: Path | None = None,
                   replay: bool = False, extra_args: tuple[str, ...] = ()) -> list[str]:
    """Expand one argv without a shell. Relative asset paths must use project_dir."""
    exe = config.raw["game"].get("exe")
    if exe and ("/" in exe or "\\" in exe):
        exe = str((config.root / exe).resolve())
    values = {"python": sys.executable, "project_dir": str(config.root), "exe": exe,
              "seed": str(seed), "map": map_name, "profile": profile, "seconds": str(duration),
              "run_dir": str(Path(run_dir).resolve()), "decider_mode": decider_mode,
              "tuning_file": str(Path(tuning_file).resolve()),
              "inputs_file": str(Path(inputs_file).resolve()) if inputs_file is not None else None}
    argv = []
    for argument in _argv(template, "command template"):
        chunks = []
        for literal, field, _, _ in _parts(argument):
            chunks.append(literal)
            if field is not None:
                if values[field] is None:
                    raise ValueError(f"command placeholder requires a value: {field}")
                chunks.append(values[field])
        argv.append("".join(chunks))
    if not argv[0]:
        raise ValueError("empty command executable")
    if ("/" in argv[0] or "\\" in argv[0]) and not Path(argv[0]).is_absolute():
        argv[0] = str((config.root / argv[0]).resolve())
    argv.extend(_argv(list(extra_args), "extra arguments", empty=True))
    return argv


def build_command(config: ProjectConfig, *, run_dir: Path, seed: int, map_name: str,
                  profile: str, decider_mode: str, duration: float, tuning_file: Path,
                  inputs_file: Path | None = None, replay: bool = False,
                  extra_args: tuple[str, ...] = ()) -> list[str]:
    template = config.replay_command if replay else config.command
    if template is None:
        raise ValueError("game.replay_command is not configured")
    if replay and inputs_file is None:
        raise ValueError("input replay requires inputs_file")
    return expand_command(config, template, run_dir=run_dir, seed=seed, map_name=map_name,
                          profile=profile, decider_mode=decider_mode, duration=duration,
                          tuning_file=tuning_file, inputs_file=inputs_file,
                          replay=replay, extra_args=extra_args)
