"""Offline provider dialect fixture serving a prevalidated planner wire document."""

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dialect", choices=("claude", "codex"), required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    options, arguments = parser.parse_known_args()
    if not options.output_json.is_absolute():
        parser.error("--output-json must be absolute")
    output = json.loads(options.output_json.read_text(encoding="utf-8"))
    # The transport must deliver the prompt through stdin in its isolated cwd.
    prompt = sys.stdin.read()
    request = json.loads(prompt.split("\n", 1)[1])
    if request["wire_schema"]["properties"]["schema_version"]["const"] != output["schema_version"]:
        raise ValueError("prompt schema does not match fixture document")
    if options.dialect == "claude":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "structured_output": output, "total_cost_usd": .03,
                          "usage": {"input_tokens": 10, "output_tokens": 3},
                          "modelUsage": {"offline-fixture": {"inputTokens": 10}}}))
        return
    destination = Path(arguments[arguments.index("-o") + 1])
    destination.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    for event in ({"type": "thread.started", "thread_id": "offline-fixture"},
                  {"type": "turn.started"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}):
        print(json.dumps(event))


if __name__ == "__main__":
    main()
