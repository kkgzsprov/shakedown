"""Offline CLI protocol fixture. Never invokes a provider."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--fixture-backend", required=True)
parser.add_argument("--case", default="success")
parser.add_argument("--capture", required=True)
options, argv = parser.parse_known_args()
prompt = sys.stdin.read()
Path(options.capture).write_text(json.dumps({"argv": argv, "prompt": prompt,
    "cwd": os.getcwd(), "files": sorted(os.listdir())}), encoding="utf-8")
case = options.case
if case == "timeout":
    time.sleep(10)
if case == "large":
    print("x" * (4 * 1024 * 1024 + 1))
    sys.exit()
if options.fixture_backend == "claude":
    if case == "malformed":
        print("{broken")
        sys.exit()
    envelope = {"type": "result", "subtype": "success", "is_error": False,
        "structured_output": {"answer": "ok"}, "total_cost_usd": 0.03,
        "usage": {"input_tokens": 10, "output_tokens": 3, "ignored": "private"},
        "modelUsage": {"observed-model": {"inputTokens": 10}}}
    if case == "missing":
        del envelope["structured_output"]
        envelope["result"] = '{"answer":"must not parse"}'
    if case == "unknown_cost":
        del envelope["total_cost_usd"]
    if case == "nan":
        envelope["total_cost_usd"] = float("nan")
    if case in {"error", "zero_error", "nonzero"}:
        envelope.update(is_error=True, subtype="error_max_budget_usd")
        if case == "zero_error":
            envelope["total_cost_usd"] = 0
    if case == "secret":
        envelope["structured_output"]["answer"] = os.environ["FIXTURE_API_KEY"]
    print(json.dumps(envelope))
    sys.exit(7 if case == "nonzero" else 0)

def emit(value):
    print(json.dumps(value), flush=True)

emit({"type": "thread.started", "thread_id": "private-resume-id"})
emit({"type": "turn.started"})
if case == "tool":
    emit({"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "private-tool-id"}})
elif case == "unknown_event":
    emit({"type": "future.effect"})
else:
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
if case == "failed":
    emit({"type": "turn.failed", "error": {"message": "failed"}})
else:
    emit({"type": "turn.completed", "usage": {"input_tokens": 10,
        "cached_input_tokens": 2, "output_tokens": 3}})
if case != "missing":
    output = Path(argv[argv.index("-o") + 1])
    output.write_text("bad json" if case == "malformed" else json.dumps({"answer": "ok"}), encoding="utf-8")
sys.exit(7 if case == "nonzero" else 0)
