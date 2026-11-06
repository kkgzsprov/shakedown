"""Bounded CLI transports; structured output is validated by the planner engine.

Claude's tools/MCP restrictions are explicit. Codex has no documented complete
tool-removal or USD-cap option, so its restricted transport requires opt-in and
rejects observable tool events. This is not a security sandbox or billing cap.

References: https://developers.openai.com/codex/noninteractive
https://developers.openai.com/codex/cli/reference
https://code.claude.com/docs/en/cli-reference
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil

from ..processes import run_process


MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class BackendResult:
    output: object | None
    reported_cost_usd: float | None
    usage: dict
    model: str | None
    metadata: dict


class BackendError(ValueError):
    def __init__(self, message: str, *, reported_cost_usd: float | None = None,
                 metadata: dict | None = None, retryable: bool = False):
        super().__init__(message)
        self.reported_cost_usd = reported_cost_usd
        self.metadata = metadata or {}
        self.retryable = retryable


def _finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class _NonFiniteJSON(ValueError):
    pass


def _reject_constant(value):
    raise _NonFiniteJSON(f"invalid JSON constant: {value}")


def _json(text: str):
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise _NonFiniteJSON("JSON number exceeds finite range")
        return number
    return json.loads(text, parse_constant=_reject_constant, parse_float=finite_float)


def _encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _redact(text: str) -> str:
    # Credentials stay in the caller's ordinary CLI auth environment. They are
    # never included in command receipts, prompts, or retained diagnostics.
    secrets = [value for key, value in os.environ.items()
               if value and len(value) >= 8 and any(part in key.upper() for part in
                                                  ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "SECRET"))]
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|wks_[A-Za-z0-9_-]{12,})\b",
                  "[REDACTED]", text)
    text = re.sub(r'("(?:thread_id|session_id)"\s*:\s*)"(?:[^"\\]|\\.)*"',
                  r'\1"[REDACTED]"', text)
    return text


def _numbers(value) -> dict:
    if type(value) is not dict:
        return {}
    result = {}
    for key, item in value.items():
        if type(key) is not str:
            continue
        if type(item) is dict:
            result[key] = _numbers(item)
        elif _finite(item) and item >= 0:
            result[key] = item
    return result


def _command(name: str, override: list[str] | None) -> list[str]:
    if override is not None:
        if (type(override) is not list or not override
                or any(type(token) is not str or "\x00" in token for token in override)
                or not override[0]):
            raise BackendError("planner command must be a nonempty argument array")
        if Path(override[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise BackendError("planner command must be a native executable or explicit interpreter, not a shell wrapper")
        return list(override)
    executable = shutil.which("claude" if name == "claude-cli" else "codex")
    if executable is None:
        raise BackendError(f"{name} executable is unavailable")
    if name == "codex-cli" and Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        # The installed npm shim delegates to this sibling script. Invoke node
        # directly rather than interpreting a batch command with user arguments.
        script = Path(executable).parent / "node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node")
        if node is None or not script.is_file():
            raise BackendError("Codex npm launcher cannot be resolved without a shell; configure its native executable")
        return [node, str(script.resolve())]
    if Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise BackendError("planner CLI requires a native executable or explicit interpreter")
    return [executable]


def _cost(envelope: dict) -> float | None:
    if "total_cost_usd" not in envelope:
        return None
    value = envelope["total_cost_usd"]
    if not _finite(value) or value < 0:
        raise BackendError("CLI reported an invalid cost")
    return float(value)


def _claude(text: str, metadata: dict, code: int) -> BackendResult:
    try:
        envelope = _json(text.strip())
    except _NonFiniteJSON as error:
        raise BackendError("Claude returned nonfinite JSON; cost is unknown", metadata=metadata) from error
    except (ValueError, TypeError) as error:
        raise BackendError("Claude did not return a JSON result envelope", metadata=metadata,
                           retryable=code == 0) from error
    if type(envelope) is not dict or envelope.get("type") != "result":
        raise BackendError("Claude did not return a result envelope", metadata=metadata, retryable=code == 0)
    try:
        cost = _cost(envelope)
    except BackendError as error:
        error.metadata = metadata
        raise
    usage = _numbers(envelope.get("usage"))
    model_usage = _numbers(envelope.get("modelUsage"))
    metadata.update(usage=usage, model_usage=model_usage,
                    provider_subtype=envelope.get("subtype") if type(envelope.get("subtype")) is str else None)
    if code != 0 or envelope.get("is_error") is not False or envelope.get("subtype") != "success":
        # Some crash envelopes initialize cost to zero; absence of billing proof
        # must not release an external-attempt reservation.
        if cost == 0:
            cost = None
        raise BackendError("Claude execution failed", reported_cost_usd=cost, metadata=metadata)
    output = envelope.get("structured_output")
    model = next(iter(model_usage)) if len(model_usage) == 1 else None
    return BackendResult(output, cost, usage, model, metadata)


def _codex(text: str, output_path: Path, metadata: dict, code: int) -> BackendResult:
    usage = {}
    model = None
    completed = False
    failed = code != 0
    tools = []
    diagnostics = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = _json(line)
        except (ValueError, TypeError) as error:
            if line.lstrip().startswith("{"):
                raise BackendError("Codex emitted malformed JSONL evidence", metadata=metadata) from error
            diagnostics += 1  # stderr is merged into the retained, redacted log.
            continue
        if type(event) is not dict or type(event.get("type")) is not str:
            raise BackendError("Codex emitted an invalid JSONL event", metadata=metadata)
        kind = event["type"]
        if type(event.get("model")) is str:
            model = event["model"]
        if kind == "turn.completed":
            raw_usage = event.get("usage")
            if type(raw_usage) is not dict or any(type(value) is not int or value < 0 for value in raw_usage.values()):
                raise BackendError("Codex emitted invalid token usage", metadata=metadata)
            usage = raw_usage
            completed = True
        elif kind in {"turn.failed", "error"}:
            failed = True
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if type(item) is not dict or type(item.get("type")) is not str:
                raise BackendError("Codex emitted an invalid item event", metadata=metadata)
            if item["type"] not in {"agent_message", "reasoning"}:
                tools.append({"type": item["type"]})
        elif kind not in {"thread.started", "turn.started"}:
            tools.append({"type": kind})
    metadata.update(usage=usage, tool_calls=tools, diagnostic_lines=diagnostics)
    if tools:
        raise BackendError("Codex used a tool or emitted an unaudited effect event", metadata=metadata)
    if failed or not completed:
        raise BackendError("Codex execution did not complete successfully", metadata=metadata)
    if not output_path.is_file():
        raise BackendError("Codex did not write its structured final message", metadata=metadata, retryable=True)
    if output_path.stat().st_size > MAX_BYTES:
        raise BackendError("Codex final message exceeds the output limit", metadata=metadata)
    output_text = _redact(output_path.read_text(encoding="utf-8"))
    output_path.write_text(output_text, encoding="utf-8")
    try:
        output = _json(output_text)
    except (ValueError, TypeError) as error:
        raise BackendError("Codex final message is not JSON", metadata=metadata, retryable=True) from error
    return BackendResult(output, None, usage, model, metadata)


def call_backend(name: str, *, prompt: str, schema: dict, work_dir: Path,
                 model: str | None, budget_usd: float, timeout_s: float = 120,
                 command: list[str] | None = None,
                 allow_restricted_codex: bool = False) -> BackendResult:
    """Run an operator-trusted executable prefix with fixed restrictions appended.

    Custom command prefixes are not inspected as a security boundary; callers
    must authorize them as executable code, including their leading options.
    """
    if name not in {"claude-cli", "codex-cli"}:
        raise BackendError("unknown planner CLI backend")
    if not _finite(budget_usd) or budget_usd <= 0 or not _finite(timeout_s) or timeout_s <= 0:
        raise BackendError("external planner budget and timeout must be finite and positive")
    if type(allow_restricted_codex) is not bool:
        raise BackendError("Codex restriction acknowledgement must be boolean")
    if name == "codex-cli" and not allow_restricted_codex:
        raise BackendError("Codex CLI has no verified tool-free mode; restricted audited execution requires explicit opt-in")
    if type(prompt) is not str or type(schema) is not dict:
        raise BackendError("planner prompt and schema have invalid types")
    if model is not None and (type(model) is not str or not model or "\x00" in model):
        raise BackendError("configured model must be a nonempty name")
    try:
        prompt_bytes, schema_bytes = prompt.encode("utf-8"), _encoded(schema)
    except (ValueError, TypeError, UnicodeError, OverflowError) as error:
        raise BackendError("planner input is not finite UTF-8 JSON") from error
    if len(prompt_bytes) > MAX_BYTES or len(schema_bytes) > MAX_BYTES:
        raise BackendError("planner input exceeds the input limit")
    prefix = _command(name, command)
    directory = Path(work_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    cwd, schema_path = directory / "empty-cwd", directory / "schema.json"
    raw_path, stdout_path, output_path = (directory / "transport-output.tmp", directory / "stdout.log",
                                           directory / "last-message.json")
    if any(path.exists() for path in (cwd, schema_path, raw_path, stdout_path, output_path, directory / "transport.json")):
        raise BackendError("planner transport evidence already exists")
    cwd.mkdir()
    schema_path.write_bytes(schema_bytes + b"\n")
    if name == "claude-cli":
        arguments = ["-p", "--output-format", "json", "--json-schema", schema_bytes.decode(),
                     "--safe-mode", "--restricted", "--tools", "", "--strict-mcp-config",
                     "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
                     "--no-session-persistence", "--max-budget-usd", str(budget_usd)]
    else:
        arguments = ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                     "--skip-git-repo-check", "--sandbox", "read-only", "--output-schema", str(schema_path),
                     "-o", str(output_path), "--json", "-c", "features.shell_tool=false",
                     "-c", "features.view_image=false", "-c", 'web_search="disabled"',
                     "-c", "skills.bundled.enabled=false", "-c", "skills.include_instructions=false"]
    if model is not None:
        arguments += ["--model", model]
    if name == "codex-cli":
        arguments.append("-")
    argv = prefix + arguments
    metadata = {"backend": name, "command": [_redact(token) for token in argv], "cwd": str(cwd),
                "command_source": "operator_trusted_override" if command is not None else "installed_cli",
                "schema_path": str(schema_path), "stdout_path": str(stdout_path),
                "isolation": "empty_cwd", "tool_policy": "claude_builtin_mcp_disabled"
                if name == "claude-cli" else "codex_restricted_audited",
                "cost_enforcement": "provider_estimate" if name == "claude-cli" else "unmetered",
                "tool_calls": [], "exit_code": None, "wall_timeout": False}
    cost = None
    try:
        code, timed_out = run_process(argv, cwd=cwd, env=dict(os.environ), stdout_path=raw_path,
                                      timeout_s=timeout_s, input_data=prompt_bytes)
        metadata.update(exit_code=code, wall_timeout=timed_out)
        with raw_path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        text = _redact(raw[:MAX_BYTES].decode("utf-8", errors="replace"))
        stdout_path.write_text(text + ("\n[output truncated]\n" if len(raw) > MAX_BYTES else ""), encoding="utf-8")
        if len(raw) > MAX_BYTES:
            raise BackendError("planner output exceeds the output limit", metadata=metadata)
        if timed_out:
            raise BackendError("planner process timed out; cost is unknown", metadata=metadata)
        result = (_claude(text, metadata, code) if name == "claude-cli"
                  else _codex(text, output_path, metadata, code))
        cost = result.reported_cost_usd
        return result
    except BackendError as error:
        error.metadata = {**metadata, **error.metadata}
        cost = error.reported_cost_usd
        raise
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError) as error:
        raise BackendError("planner transport could not read or execute the CLI", metadata=metadata) from error
    finally:
        if raw_path.is_file():
            raw_path.unlink()
        # Output files can contain provider diagnostics even on an unsuccessful
        # attempt. Retain only redacted evidence, never raw credential strings.
        if output_path.is_file():
            with output_path.open("rb") as stream:
                output_raw = stream.read(MAX_BYTES + 1)
            output_path.write_text(_redact(output_raw[:MAX_BYTES].decode("utf-8", errors="replace")), encoding="utf-8")
        receipt = {**metadata, "reported_cost_usd": cost}
        (directory / "transport.json").write_bytes(_encoded(receipt) + b"\n")
