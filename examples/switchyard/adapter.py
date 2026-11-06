"""One candidate maps to one ordinary button; no separate bot movement path."""

from __future__ import annotations

from collections import deque
import random
import sys

from shakedown.deciders.tape import TapeDecider
from shakedown.protocol import PREFIX, canonical_json, parse_answer

from .game import MOVES


class Adapter:
    def __init__(self, game, mode):
        if mode not in {"rule", "bridge"} and not mode.startswith("tape:"):
            raise ValueError("decider must be rule, bridge, or tape:PATH")
        self.game, self.mode, self.seq = game, mode, 0
        self.memory = set()
        self.tape = TapeDecider(mode[5:]) if mode.startswith("tape:") else None

    @staticmethod
    def send(message):
        print(PREFIX + canonical_json(message).decode("utf-8"), flush=True)

    def hello(self):
        if self.mode == "bridge":
            self.send({"v": 1, "type": "hello", "game": "switchyard", "adapter_version": "1",
                       "seed": self.game.seed, "profile": self.game.config["profile"]})

    def request(self):
        game = self.game
        order_seed = game.seed * 1009 + game.step
        names = game.legal_buttons()
        random.Random(order_seed).shuffle(names)
        descriptions = {"wait": "Wait for one turn.", "collect": "Collect the parcel at this depot.",
                        "deliver": "Deliver the carried parcel at this dock."}
        state = game.observation()
        self.memory.update(tuple(item["position"]) for item in state["seen"])
        state["memory"] = [list(position) for position in sorted(self.memory)]
        return {"v": 1, "type": "decide", "run": "switchyard", "seq": self.seq,
                "step": game.step, "t": game.time, "reason": "turn_ready",
                "profile": game.config["profile"], "state": state,
                "candidates": {name: descriptions.get(name, f"Press {name} to move one tile.") for name in names},
                "order_seed": order_seed}

    @staticmethod
    def rule(request):
        state = request["state"]
        actions = request["candidates"]
        if "deliver" in actions:
            return "deliver"
        if "collect" in actions:
            return "collect"
        start = tuple(state["self"]["position"])
        target = tuple(state["objective"]["dock" if state["self"]["cargo"] else "depot"])
        blocked = {tuple(position) for position in state["public_map"]["walls"]}
        blocked.update(tuple(item["position"]) for item in state["seen"])
        blocked.update(tuple(position) for position in state["memory"])
        width, height = state["public_map"]["size"]
        queue, visited = deque([(start, None)]), {start}
        while queue:
            position, first = queue.popleft()
            if position == target:
                return first or "wait"
            for button, (dx, dy) in MOVES.items():
                nxt = position[0] + dx, position[1] + dy
                if (0 <= nxt[0] < width and 0 <= nxt[1] < height
                        and nxt not in blocked and nxt not in visited):
                    visited.add(nxt)
                    queue.append((nxt, first or button))
        return "wait"

    def tick(self):
        self.seq += 1
        request = self.request()
        if self.mode == "bridge":
            self.send(request)
            choice = parse_answer(sys.stdin.readline(), request)["choice"]
        elif self.tape:
            choice = self.tape.decide(request, "").choice
        else:
            choice = self.rule(request)
        self.game.emit("decision", seq=self.seq, reason="turn_ready", choice=choice, status="selected")
        self.game.emit("decision", seq=self.seq, reason="turn_ready", choice=choice,
                       status="applied", applied_step=self.game.step)
        return {"button": choice}

    def close(self):
        if self.tape:
            self.tape.close()
        if self.mode == "bridge":
            message = {"v": 1, "type": "end", "outcome": self.game.outcome, "step": self.game.step}
            if self.game.oracle:
                message["oracle"] = self.game.oracle
            self.send(message)
