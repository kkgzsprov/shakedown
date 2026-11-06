"""Reference game adapter: observations and tactics become ordinary tick inputs.

The simulation never calls this module. Input replay therefore exercises the
same collision, combat, and oracle code without an executor or decider.
"""

from __future__ import annotations

import math
import random
import sys

from shakedown.deciders.tape import TapeDecider, TapeDivergence
from shakedown.protocol import PREFIX, canonical_json, parse_answer

from .game import Game


PROFILES = {
    "cautious": {"decision_delay": 18, "reaction": 18, "jitter": 0.10},
    "aggressive": {"decision_delay": 12, "reaction": 10, "jitter": 0.18},
    "looter": {"decision_delay": 21, "reaction": 15, "jitter": 0.14},
}


def distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Adapter:
    def __init__(self, game: Game, profile: str = "cautious", mode: str = "rule"):
        self.game, self.profile, self.mode = game, profile, mode
        self.limits = PROFILES[profile]
        self.seq = 0
        self.last_decision = -180
        self.last_snapshot = None
        self.memory: dict[str, dict] = {}
        self.first_seen: dict[str, int] = {}
        self.recent: list[dict] = []
        self.visited: set[str] = set()
        self.active = "hold"
        self.active_target = None
        self.active_started = 0
        self.active_position = self.position
        self.path: list[tuple[float, float]] = []
        self.path_step = -60
        self.route_step = -60
        self.route = []
        self.pending = None
        self.finished = False
        self.tape = TapeDecider(mode[5:]) if mode.startswith("tape:") else None
        # Deliberately broken demonstration: extra host latency changes load completion.
        # Kept entirely out of the clean simulation and its RNG.
        if game.bug == "wallclock_load":
            import time
            self.load_started = time.monotonic()
            self.load_complete = False

    @property
    def position(self):
        return self.game.player.x, self.game.player.y

    def hello(self) -> None:
        if self.mode == "bridge":
            self.emit_wire({"v": 1, "type": "hello", "game": "tinyraid",
                            "adapter_version": "0.1.0", "seed": self.game.seed,
                            "profile": self.profile})

    @staticmethod
    def emit_wire(message: dict) -> None:
        sys.stdout.buffer.write(PREFIX.encode() + canonical_json(message) + b"\n")
        sys.stdout.buffer.flush()

    def visible(self):
        if self.game.bug == "vision_leak":
            return [guard for guard in self.game.guards if guard.hp > 0]
        return self.game.visible_guards()

    def observe(self) -> dict:
        game, player = self.game, self.game.player
        seen = []
        visible = self.visible()
        for guard in visible:
            self.first_seen.setdefault(guard.id, game.step)
            bearing = math.degrees(math.atan2(guard.y - player.y, guard.x - player.x)
                                   - player.angle)
            seen.append({"id": guard.id, "bearing": round((bearing + 180) % 360 - 180, 2),
                         "range_m": round(distance(self.position, (guard.x, guard.y)), 3),
                         "exposure": "full_body",
                         "since_s": (game.step - self.first_seen[guard.id]) / 60})
            self.memory[guard.id] = {"id": guard.id, "x": round(guard.x, 3),
                                     "y": round(guard.y, 3), "step": game.step}
        ids = {guard.id for guard in visible}
        self.first_seen = {key: value for key, value in self.first_seen.items() if key in ids}
        self.memory = {key: value for key, value in self.memory.items()
                       if game.step - value["step"] <= 600}
        memory = [{"id": key, "last_position": [item["x"], item["y"]],
                   "last_seen_s": (game.step - item["step"]) / 60}
                  for key, item in sorted(self.memory.items()) if key not in ids]
        cues = []
        for cue in game.cues:
            item = {"kind": cue.get("kind", "sound")}
            if "x" in cue and "y" in cue:
                item["bearing"] = round(math.degrees(math.atan2(
                    cue["y"] - player.y, cue["x"] - player.x)), 2)
            cues.append(item)
        if game.step - self.route_step >= 30:
            self.route = game.path_to(game.exit_pos)
            self.route_step = game.step
        route = self.route
        points = [self.position, *route]
        route_length = sum(distance(a, b) for a, b in zip(points, points[1:]))
        return {
            "self": {"position": [round(player.x, 3), round(player.y, 3)],
                     "hp": player.hp, "max_hp": player.max_hp, "stance": player.stance,
                     "stamina": round(player.stamina, 3)},
            "equipment": {"mag": player.ammo, "reserve": player.reserve,
                          "heals": player.heals, "key": player.key},
            "seen": seen, "memory": memory, "cues": cues,
            "objective": {"goal": "extract", "route_m": round(route_length, 3),
                          "route_available": bool(route) or distance(self.position, game.exit_pos) < .5,
                          "time_left_s": max(0, game.duration - game.time),
                          "explore_time_left_s": max(0, game.duration * .55 - game.time)},
            "recent": [{**{key: value for key, value in item.items() if key != "step"},
                        "seconds_ago": (game.step - item["step"]) / 60}
                       for item in self.recent[-4:]],
        }

    def candidates(self) -> tuple[dict, dict]:
        game, player = self.game, self.game.player
        descriptions, targets = {}, {}

        def add(name, description, target=None):
            descriptions[name] = description
            targets[name] = target

        if player.hp < player.max_hp and player.heals > 0:
            add("heal", f"Heal. Health {player.hp}/{player.max_hp}, {player.heals} kits")
        if player.ammo < 8 and player.reserve > 0:
            add("reload", f"Reload. {player.ammo} rounds loaded, {player.reserve} reserve")
        for guard in self.visible():
            if player.ammo > 0:
                add(f"engage_{guard.id}", f"Engage {guard.id}. "
                    f"{distance(self.position, (guard.x, guard.y)):.1f} m, {player.ammo} rounds", guard.id)
        for key, point in game.covers.items():
            if distance(self.position, point) > .4 and game.path_to(point):
                add(f"cover_{key}", f"Move to cover {key}. "
                    f"{distance(self.position, point):.1f} m", point)
        # Retreat toward the known spawn side, using only currently observed threats.
        retreat = (2.0, 2.0)
        if distance(self.position, retreat) > .5 and game.path_to(retreat):
            add("withdraw", f"Withdraw to entry. {distance(self.position, retreat):.1f} m", retreat)
        for key, point in game.waypoints.items():
            if distance(self.position, point) < .5:
                self.visited.add(key)
        unvisited = [(key, point) for key, point in game.waypoints.items() if key not in self.visited]
        if unvisited:
            key, point = min(unvisited, key=lambda item: distance(self.position, item[1]))
            if game.path_to(point):
                add(f"search_{key}", f"Search room {key}. Not visited, "
                    f"{distance(self.position, point):.1f} m", point)
        for key, crate in game.crates.items():
            point = (crate["x"], crate["y"])
            if not crate["looted"] and (game.path_to(point) or distance(self.position, point) < 1):
                add(f"loot_{key}", f"Loot crate {key}. {distance(self.position, point):.1f} m", point)
                break
        door = game.door
        door_entry = (10.0 if player.x < door["x"] else 13.0, door["y"])
        if not door["open"] and player.key and game.path_to(door_entry):
            add("use_door", f"Move to and unlock door. "
                f"{distance(self.position, door_entry):.1f} m", door_entry)
        if game.path_to(game.exit_pos) or distance(self.position, game.exit_pos) < 1:
            add("extract", f"Move to extraction. {distance(self.position, game.exit_pos):.1f} m", game.exit_pos)
        # Preserve emergency options and leave a slot for hold even on crowded maps.
        names = list(descriptions)[:11]
        descriptions = {name: descriptions[name] for name in names}
        targets = {name: targets[name] for name in names}
        descriptions["hold"] = "Keep the current action"
        targets["hold"] = None
        return descriptions, targets

    def rule(self, request: dict) -> str:
        names = request["candidates"]
        state = request["state"]
        if state["self"]["hp"] < 60 and "heal" in names:
            return "heal"
        if state["equipment"]["mag"] <= 1 and "reload" in names:
            return "reload"
        if state["self"]["hp"] < 30 and "withdraw" in names:
            return "withdraw"
        engage = [name for name in names if name.startswith("engage_")]
        if engage:
            return engage[0]
        if "use_door" in names:
            return "use_door"
        if state["objective"]["explore_time_left_s"] <= 0 and "extract" in names:
            return "extract"
        loot = [name for name in names if name.startswith("loot_")]
        search = [name for name in names if name.startswith("search_")]
        if self.profile == "looter" and loot:
            return loot[0]
        if search:
            return search[0]
        if loot:
            return loot[0]
        return "extract" if "extract" in names else "hold"

    def reason(self, state: dict) -> str | None:
        if self.pending is not None:
            return None
        previous = self.last_snapshot
        damage = previous is not None and state["self"]["hp"] < previous["self"]["hp"]
        if self.game.step - self.last_decision < 30 and not damage:
            return None
        if previous is None:
            return "start"
        if damage:
            return "damage_taken"
        ids = {item["id"] for item in state["seen"]}
        old_ids = {item["id"] for item in previous["seen"]}
        if ids != old_ids:
            return "new_contact" if ids - old_ids else "lost_contact"
        if self.finished:
            return "tactic_complete"
        interval = 30 if ids else 180
        if self.game.step - self.last_decision >= interval:
            return "timer"
        return None

    def decide(self, state: dict, reason: str) -> None:
        self.seq += 1
        choices, targets = self.candidates()
        order_seed = self.game.seed * 65537 + self.seq
        names = list(choices)
        random.Random(order_seed).shuffle(names)
        request = {"v": 1, "type": "decide", "run": "tinyraid", "seq": self.seq,
                   "step": self.game.step, "t": self.game.time, "reason": reason,
                   "profile": self.profile, "state": state,
                   "candidates": {name: choices[name] for name in names}, "order_seed": order_seed}
        if self.mode == "bridge":
            self.emit_wire(request)
            choice = parse_answer(sys.stdin.buffer.readline().decode("utf-8"), request)["choice"]
        elif self.tape is not None:
            choice = self.tape.decide(request, "").choice
        else:
            choice = self.rule(request)
        self.pending = {"seq": self.seq, "choice": choice, "target": targets[choice],
                        "reason": reason, "apply_at": self.game.step + self.limits["decision_delay"]}
        self.game.emit("decision", seq=self.seq, reason=reason, choice=choice, status="selected")
        self.last_snapshot, self.last_decision = state, self.game.step
        self.finished = False

    def end_tactic(self, ended: str) -> None:
        if self.active != "hold":
            self.recent.append({"tactic": self.active, "ended": ended,
                                "seconds": (self.game.step - self.active_started) / 60,
                                "moved_m": round(distance(self.position, self.active_position), 3),
                                "step": self.game.step})
            self.recent = self.recent[-4:]
        self.active, self.active_target, self.path = "hold", None, []
        self.finished = True

    def apply_pending(self) -> None:
        pending = self.pending
        if pending is None or self.game.step < pending["apply_at"]:
            return
        self.pending = None
        choices, _ = self.candidates()
        receipt = {key: pending[key] for key in ("seq", "choice", "reason")}
        if pending["choice"] not in choices:
            self.game.emit("decision", **receipt, status="dropped", drop_reason="no_longer_legal")
            self.end_tactic("dropped")
            self.recent.append({"tactic": pending["choice"], "ended": "dropped", "seconds": 0,
                                "moved_m": 0, "step": self.game.step})
            return
        self.game.emit("decision", **receipt, status="applied", applied_step=self.game.step)
        if pending["choice"] != "hold" and pending["choice"] != self.active:
            self.end_tactic("replaced")
            self.active, self.active_target = pending["choice"], pending["target"]
            self.active_started, self.active_position = self.game.step, self.position
            self.path_step = -60
        self.finished = False

    def movement(self, target) -> dict:
        if distance(self.position, target) < .25:
            return {}
        if not self.path or self.game.step - self.path_step >= 30:
            self.path = self.game.path_to(target)
            self.path_step = self.game.step
        while self.path and distance(self.position, self.path[0]) < .2:
            self.path.pop(0)
        if not self.path:
            self.end_tactic("failed")
            return {}
        x, y = self.path[0]
        dx, dy = x - self.position[0], y - self.position[1]
        length = math.hypot(dx, dy)
        return {"move": [dx / length, dy / length], "aim": [x, y],
                "sprint": self.profile == "aggressive"}

    def execute(self) -> dict:
        game, player = self.game, self.game.player
        name, target = self.active, self.active_target
        inputs: dict = {"stance": "stand"}
        if name.startswith("engage_"):
            guard = next((guard for guard in self.visible() if guard.id == target), None)
            if guard is None or player.ammo <= 0:
                self.end_tactic("done")
            else:
                ready = game.step - self.first_seen.get(guard.id, game.step) >= self.limits["reaction"]
                rng = random.Random(game.seed * 1000003 + game.step)
                jitter = self.limits["jitter"]
                aim = [guard.x + rng.uniform(-jitter, jitter), guard.y + rng.uniform(-jitter, jitter)]
                inputs.update(aim=aim, fire=ready)
        elif name == "use_door":
            if distance(self.position, (game.door["x"], game.door["y"])) <= 1.7:
                inputs["use_door"] = True
            else:
                inputs.update(self.movement(target))
            if game.door["open"]:
                self.end_tactic("done")
                self.route_step = -60
        elif name in {"heal", "reload"}:
            inputs[name] = True
            complete = ((name == "heal" and (player.hp >= player.max_hp or player.heals <= 0))
                        or (name == "reload" and (player.ammo >= 8 or player.reserve <= 0)))
            if complete:
                self.end_tactic("done")
                inputs.pop(name, None)
        elif target is not None:
            inputs.update(self.movement(target))
            if name.startswith("loot_") and distance(self.position, target) < .9:
                inputs["loot"] = True
                key = name[5:]
                if game.crates[key]["looted"]:
                    self.end_tactic("done")
            elif name == "extract" and distance(self.position, target) < .9:
                inputs["extract"] = True
            elif not name.startswith("loot_") and name != "extract" and distance(self.position, target) < .35:
                self.end_tactic("done")
        else:
            # Looking around is an input, so it is also captured for input replay.
            angle = (game.step // 90) * math.pi / 2
            inputs["aim"] = [player.x + math.cos(angle) * 5, player.y + math.sin(angle) * 5]
        if game.step - self.active_started > 360 and self.active != "hold":
            self.end_tactic("failed")
        # Reflex only uses warned hazards. It overrides movement without replacing the tactic.
        for grenade in game.grenades:
            if not grenade["warned"]:
                continue
            age = game.step - grenade.get("spawn_step", game.step)
            if age < self.limits["reaction"]:
                continue
            dx, dy = player.x - grenade["x"], player.y - grenade["y"]
            length = math.hypot(dx, dy)
            if length < 3.5:
                if length < .01:
                    dx, dy, length = 1, 0, 1
                inputs.update(move=[dx / length, dy / length], sprint=True)
                break
        if game.scenario == "stuck":
            inputs["move"] = [1.0, 0.0]
        return inputs

    def tick(self) -> dict:
        if self.game.bug == "wallclock_load":
            import time
            if not self.load_complete and time.monotonic() - self.load_started >= .15:
                self.load_complete = True
                self.game.emit("asset_loaded")
        self.apply_pending()
        state = self.observe()
        reason = self.reason(state)
        if reason:
            self.decide(state, reason)
        return self.execute()

    def close(self) -> None:
        if self.tape is not None and self.tape.cursor != len(self.tape.records):
            raise TapeDivergence("unused decisions remain at end of tinyraid tape")
        if self.mode == "bridge":
            self.emit_wire({"v": 1, "type": "end", "outcome": self.game.outcome,
                            "step": self.game.step,
                            **({"oracle": self.game.oracle} if self.game.oracle else {})})
