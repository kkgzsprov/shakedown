"""Fixed-step tinyraid rules, independent of policy and the wire protocol.

``tick`` is the single input path for live play, the executor, and replay.
The simulation never reads a wall clock and owns its own seeded random stream.
Distances are metres; angles are radians; one tick is exactly 1/60 second.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import heapq
import json
import math
from pathlib import Path
import random


HZ = 60
DT = 1 / HZ
BUGS = (
    "corner_pocket", "thin_wall", "ammo_underflow", "door_no_key",
    "heal_overflow", "silent_grenade", "extract_crouch", "vision_leak",
    "wallclock_load",
)
SCENARIOS = (
    "standard", "stuck", "thin_wall", "ammo_underflow", "heal_overflow",
    "door_no_key", "grenade", "extraction", "vision", "corner_pocket",
)


def _tuning_number(value) -> bool:
    return type(value) is int or type(value) is float and math.isfinite(value)


def load_tuning_keys(path: Path) -> dict:
    """Load the game's bounds and defaults from its exposed key table."""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate tuning table key: {key}")
            value[key] = item
        return value

    def reject_constant(value):
        raise ValueError(f"nonfinite tuning table value: {value}")

    data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=reject_constant)
    if type(data) is not dict or set(data) != {"keys"} or type(data["keys"]) is not dict or not data["keys"]:
        raise ValueError("tuning table must contain a nonempty keys object")
    for key, spec in data["keys"].items():
        if (not key or "\x00" in key or type(spec) is not dict
                or not {"type", "min", "max", "default"} <= spec.keys()
                or type(spec["type"]) is not str or spec["type"] not in {"int", "float"}):
            raise ValueError(f"invalid tuning specification: {key}")
        values = [spec[field] for field in ("min", "max", "default")]
        if (not all(_tuning_number(value) for value in values)
                or spec["type"] == "int" and any(type(value) is not int for value in values)
                or not spec["min"] <= spec["default"] <= spec["max"]):
            raise ValueError(f"invalid tuning bounds or default: {key}")
    return data["keys"]


TUNING_KEYS = load_tuning_keys(Path(__file__).with_name("tuning_keys.json"))


def distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle_difference(a: float, b: float) -> float:
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def segment_hits_rect(a, b, rect, margin: float = 0.0) -> bool:
    """Closed segment versus an axis-aligned rectangle (slab clipping)."""
    low, high = 0.0, 1.0
    for start, end, minimum, maximum in (
        (a[0], b[0], rect[0] - margin, rect[2] + margin),
        (a[1], b[1], rect[1] - margin, rect[3] + margin),
    ):
        delta = end - start
        if abs(delta) < 1e-12:
            if start < minimum or start > maximum:
                return False
        else:
            entry, leave = sorted(((minimum - start) / delta, (maximum - start) / delta))
            low, high = max(low, entry), min(high, leave)
            if low > high:
                return False
    return True


@dataclass
class Player:
    x: float = 2.5
    y: float = 2.5
    hp: float = 100.0
    max_hp: float = 100.0
    ammo: int = 8
    reserve: int = 24
    heals: int = 2
    key: bool = False
    stance: str = "stand"
    stamina: float = 100.0
    angle: float = 0.0
    loot: int = 0


@dataclass
class Guard:
    id: str
    x: float
    y: float
    angle: float = 0.0
    hp: float = 60.0
    mode: str = "patrol"
    last_seen: tuple[float, float] | None = None
    lost_step: int = 0
    next_shot: int = 0
    next_grenade: int = 0
    patrol_index: int = 0


class Game:
    """One self-contained run. Policy may read this object but only tick inputs act."""

    width = 24.0
    height = 18.0
    radius = 0.18

    def __init__(self, seed: int = 1, map_name: str = "yard", duration: float = 30,
                 bug: str | None = None, scenario: str = "standard",
                 tuning: dict | None = None):
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        if map_name not in {"yard", "warehouse"}:
            raise ValueError("unknown map")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration must be finite and positive")
        if bug is not None and bug not in BUGS:
            raise ValueError("unknown bug")
        if scenario not in SCENARIOS:
            raise ValueError("unknown scenario")
        self.tuning = {key: spec["default"] for key, spec in TUNING_KEYS.items()}
        for key, value in (tuning or {}).items():
            if key not in TUNING_KEYS:
                raise ValueError(f"unknown tuning key: {key}")
            spec = TUNING_KEYS[key]
            if (not _tuning_number(value) or spec["type"] == "int" and type(value) is not int
                    or not spec["min"] <= value <= spec["max"]):
                raise ValueError(f"out-of-range tuning value: {key}")
            self.tuning[key] = value
        self.seed, self.map_name, self.duration = seed, map_name, duration
        self.bug, self.scenario = bug, scenario
        self.rng = random.Random(seed)
        self.step = 0
        self.max_steps = max(1, math.ceil(duration * HZ))
        self.player = Player()
        self.walls = [(11.0, 0.0, 12.0, 7.0), (11.0, 10.0, 12.0, 18.0),
                      (5.0, 8.0, 7.0, 9.0), (16.0, 4.0, 18.0, 5.0)]
        if map_name == "warehouse":
            self.walls += [(2.0, 12.0, 5.0, 13.0), (18.0, 12.0, 20.0, 13.0)]
        self.door = {"x": 11.5, "y": 8.5, "open": False}
        self.covers = {"c1": (4.5, 7.5), "c2": (8.5, 9.5), "c3": (15.5, 5.5)}
        self.crates = {
            "c1": {"x": 5.5, "y": 4.5, "key": True, "value": 10, "looted": False},
            "c2": {"x": 19.5, "y": 8.5, "key": False, "value": 50, "looted": False},
        }
        self.waypoints = {"r1": (7.5, 4.5), "r2": (8.5, 13.5), "r3": (19.5, 10.5)}
        self.exit_pos = (21.5, 15.5)
        self.guards = [Guard("g1", 8.5, 12.5, -math.pi / 2),
                       Guard("g2", 18.5, 9.5, math.pi)]
        for guard in self.guards:
            guard.next_shot = 90 + self.rng.randrange(60)
            guard.next_grenade = 420 + self.rng.randrange(120)
        self.grenades: list[dict] = []
        self.cues: list[dict] = []
        self.events: list[dict] = []
        self.counts: Counter = Counter()
        self.outcome: str | None = None
        self.oracle: str | None = None
        self.distance_walked = 0.0
        self._reload_until = self._heal_until = self._next_shot = 0
        self._extract_steps = 0
        self.extraction_attempts = 0  # Button-held ticks while inside the zone.
        self.extract_zone_steps = 0  # Actual zone dwell, including no-button ticks.
        self._loot_opportunities = set()  # Count each reachable nearby crate once.
        self._door_without_key = False
        self._trapped = scenario == "stuck"
        self._movement: deque = deque(maxlen=601)
        self._last_new_cell = self._last_contact = 0
        self._visited = set()
        self._warned_progress = False
        self._contacts = set()
        self._scenario_setup()
        self.emit("run_begin", seed=seed, map=map_name, bug=bug, scenario=scenario,
                  tuning=self.tuning)
        if scenario == "grenade":
            self._grenade(self.player.x, self.player.y, "g1", fuse=90)

    @property
    def time(self) -> float:
        return self.step / HZ

    @property
    def position(self) -> tuple[float, float]:
        return self.player.x, self.player.y

    @property
    def distance(self) -> float:
        return self.distance_walked

    def _scenario_setup(self) -> None:
        if self.scenario != "standard":
            self.guards = []
        p = self.player
        if self.scenario == "stuck":
            p.x, p.y = 3.5, 3.5
        elif self.scenario == "corner_pocket":
            p.x, p.y = 10.6, 6.6
        elif self.scenario == "thin_wall":
            p.x, p.y = 10.7, 5.5
        elif self.scenario == "ammo_underflow":
            p.ammo = 1
        elif self.scenario == "door_no_key":
            p.x, p.y = 12.5, 8.5
        elif self.scenario == "grenade":
            p.x, p.y = 5.5, 5.5
        elif self.scenario == "extraction":
            p.x, p.y = self.exit_pos
        elif self.scenario == "vision":
            p.x, p.y, p.angle = 9.5, 5.5, 0.0
            self.guards = [Guard("g1", 13.5, 5.5, math.pi)]

    def emit(self, event_type: str, **fields) -> None:
        # Numeric-oracle evidence must itself remain valid JSON, even when the
        # failing world state contains NaN/Infinity. Preserve those as strings.
        def json_value(value):
            if isinstance(value, float) and not math.isfinite(value):
                return str(value)
            if isinstance(value, dict):
                return {key: json_value(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_value(item) for item in value]
            return value

        self.events.append({"t": self.time, "step": self.step, "type": event_type,
                            **json_value(fields)})

    def solids(self) -> list[tuple]:
        return self.walls + ([] if self.door["open"] else [(11.0, 7.0, 12.0, 10.0)])

    def walkable(self, point) -> bool:
        x, y = point
        return (self.radius <= x <= self.width - self.radius
                and self.radius <= y <= self.height - self.radius
                and not any(rect[0] - self.radius < x < rect[2] + self.radius
                            and rect[1] - self.radius < y < rect[3] + self.radius
                            for rect in self.solids()))

    def line_of_sight(self, a, b) -> bool:
        return not any(segment_hits_rect(a, b, rect) for rect in self.solids())

    def visible_guards(self) -> list[Guard]:
        p = self.player
        return [g for g in self.guards if g.hp > 0
                and distance(self.position, (g.x, g.y)) <= 14
                and abs(angle_difference(math.atan2(g.y - p.y, g.x - p.x), p.angle)) <= math.pi / 2
                and self.line_of_sight(self.position, (g.x, g.y))]

    def path_to(self, target, start=None) -> list[tuple[float, float]]:
        """A* on metre cells, followed by exact continuous target coordinates."""
        start = self.position if start is None else start
        if not self.walkable(target):
            return []
        if self.line_of_sight(start, target) and not any(
            segment_hits_rect(start, target, wall, self.radius) for wall in self.solids()
        ):
            return [tuple(target)]
        first = (int(start[0]), int(start[1]))
        goal = (int(target[0]), int(target[1]))
        frontier = [(0, first)]
        costs, previous = {first: 0}, {}
        while frontier:
            _, node = heapq.heappop(frontier)
            if node == goal:
                cells = []
                while node != first:
                    cells.append((node[0] + 0.5, node[1] + 0.5))
                    node = previous[node]
                cells.reverse()
                return cells + ([tuple(target)] if not cells or cells[-1] != tuple(target) else [])
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbour = (node[0] + dx, node[1] + dy)
                point = (neighbour[0] + 0.5, neighbour[1] + 0.5)
                if not self.walkable(point):
                    continue
                new_cost = costs[node] + 1
                if new_cost < costs.get(neighbour, math.inf):
                    costs[neighbour] = new_cost
                    previous[neighbour] = node
                    heuristic = abs(neighbour[0] - goal[0]) + abs(neighbour[1] - goal[1])
                    heapq.heappush(frontier, (new_cost + heuristic, neighbour))
        return []

    def _move(self, actor, dx: float, dy: float, speed: float, ignore_walls=False) -> None:
        length = math.hypot(dx, dy)
        if length > 1:
            dx, dy = dx / length, dy / length
        amount = speed * DT
        for attr, delta in (("x", dx * amount), ("y", dy * amount)):
            point = (actor.x + delta, actor.y) if attr == "x" else (actor.x, actor.y + delta)
            if ignore_walls or self.walkable(point):
                setattr(actor, attr, getattr(actor, attr) + delta)

    def _sound(self, kind: str, x: float, y: float, **fields) -> None:
        cue = {"kind": kind, "x": x, "y": y, "step": self.step, **fields}
        self.cues.append(cue)
        self.emit("sound", **cue)

    def _grenade(self, x: float, y: float, owner: str, fuse: int = 120) -> None:
        warned = not (self.bug == "silent_grenade" and owner == "g1")
        self.grenades.append({"x": x, "y": y, "spawn_step": self.step, "explode_step": self.step + fuse,
                              "owner": owner, "warned": warned})
        self.emit("grenade", x=x, y=y, owner=owner, warned=warned)
        if warned:
            self._sound("grenade_warning", x, y, explode_step=self.step + fuse)

    def _damage_player(self, amount: float, cause: str, source: str, warned=True) -> None:
        self.player.hp = max(0.0, self.player.hp - amount)
        self.counts["damage_taken"] += amount
        self.emit("damage_taken", amount=amount, cause=cause, source=source, warned=warned,
                  hp=self.player.hp)
        if self.player.hp <= 0:
            if cause == "explosion" and not warned:
                self.counts["unwarned_explosion_deaths"] += 1
            self.emit("death", cause=cause, source=source, warned=warned)
            self._finish("death")

    def _player_actions(self, inputs: dict) -> None:
        p = self.player
        if self._reload_until and self.step >= self._reload_until:
            count = min(8 - p.ammo, p.reserve)
            p.ammo += count
            p.reserve -= count
            self._reload_until = 0
            self.emit("reload", ammo=p.ammo, reserve=p.reserve)
        if self._heal_until and self.step >= self._heal_until:
            p.hp = p.hp + 25 if self.bug == "heal_overflow" else min(p.max_hp, p.hp + 25)
            p.heals -= 1
            self._heal_until = 0
            self.counts["heals"] += 1
            self.emit("heal", hp=p.hp, remaining=p.heals)
        if inputs.get("reload") and not self._reload_until and p.ammo < 8 and p.reserve > 0:
            self._reload_until = self.step + HZ
            self.emit("reload_begin")
        if inputs.get("heal") and not self._heal_until and p.heals > 0 and (
            p.hp < p.max_hp or self.bug == "heal_overflow"
        ):
            self._heal_until = self.step + 2 * HZ
            self.emit("heal_begin")
        if inputs.get("fire") and self._reload_until and self.bug == "ammo_underflow":
            p.ammo = -1
        if inputs.get("fire") and self.step >= self._next_shot and p.ammo > 0 and not self._reload_until and not self._heal_until:
            p.ammo -= 1
            self._next_shot = self.step + 12
            self.counts["shots"] += 1
            self.emit("shot", x=p.x, y=p.y, angle=p.angle)
            self._sound("gunshot", p.x, p.y)
            targets = sorted(self.guards, key=lambda g: distance(self.position, (g.x, g.y)))
            for g in targets:
                if (g.hp > 0 and distance(self.position, (g.x, g.y)) <= 14
                        and self.line_of_sight(self.position, (g.x, g.y))
                        and abs(angle_difference(math.atan2(g.y - p.y, g.x - p.x), p.angle)) <= 0.10):
                    g.hp = max(0.0, g.hp - 30)
                    self.counts["hits"] += 1
                    self.emit("hit", target=g.id, damage=30, hp=g.hp)
                    if g.hp == 0:
                        self.counts["kills"] += 1
                        self.emit("guard_down", target=g.id)
                    break
        if inputs.get("loot"):
            for name, crate in self.crates.items():
                if not crate["looted"] and distance(self.position, (crate["x"], crate["y"])) < 1.2:
                    crate["looted"] = True
                    p.key = p.key or crate["key"]
                    p.loot += crate["value"]
                    self.counts["loot"] += crate["value"]
                    self.emit("loot", crate=name, value=crate["value"], key=crate["key"])
        if inputs.get("use_door") and not self.door["open"] and distance(self.position, (11.5, 8.5)) <= 1.7:
            if p.key or (self.bug == "door_no_key" and p.x > 11.5):
                self.door["open"] = True
                self._door_without_key = not p.key
                self.emit("door_open", key=p.key)
        if inputs.get("extract") and distance(self.position, self.exit_pos) < 1.0:
            if not (self.bug == "extract_crouch" and p.stance == "crouch"):
                self._extract_steps += 1
        else:
            self._extract_steps = 0

    def _guards_tick(self, inputs: dict) -> None:
        p = self.player
        for g in self.guards:
            if g.hp <= 0:
                continue
            d = distance(self.position, (g.x, g.y))
            angle = math.atan2(p.y - g.y, p.x - g.x)
            visible = (d < self.tuning["guard.vision"] and self.line_of_sight((g.x, g.y), self.position)
                       and abs(angle_difference(angle, g.angle)) < math.pi / 3)
            heard = next((cue for cue in reversed(self.cues)
                          if cue["step"] == self.step
                          and cue["kind"] in {"gunshot", "footsteps"}
                          and distance((g.x, g.y), (cue["x"], cue["y"]))
                          < (9 if cue["kind"] == "gunshot" else 4.5)), None)
            if visible or heard is not None:
                g.last_seen = self.position if visible else (heard["x"], heard["y"])
                g.lost_step = self.step
                g.mode = "chase" if visible else "search"
            elif g.mode == "chase":
                g.mode = "search"
            elif g.mode == "search" and self.step - g.lost_step > 4 * HZ:
                g.mode, g.last_seen = "patrol", None
            if visible:
                self._last_contact = self.step
                if g.id not in self._contacts:
                    self._contacts.add(g.id)
                    self.counts["contacts"] += 1
                    self.emit("contact", guard=g.id, range_m=d, observer="guard")
                g.angle = angle
                if self.step >= g.next_shot and d < 12:
                    g.next_shot = self.step + 60
                    self.emit("guard_shot", guard=g.id)
                    if self.rng.random() < 0.55:
                        self._damage_player(self.tuning["guard.damage"], "bullet", g.id)
                if self.outcome:
                    return
                if self.step >= g.next_grenade and d < 10:
                    g.next_grenade = self.step + 8 * HZ
                    self._grenade(p.x, p.y, g.id)
            if g.mode == "patrol":
                points = [(8.5, 12.5), (8.5, 3.5)] if g.id == "g1" else [(18.5, 9.5), (21.5, 14.5)]
                target = points[g.patrol_index % 2]
                if distance((g.x, g.y), target) < 0.3:
                    g.patrol_index += 1
                    target = points[g.patrol_index % 2]
            else:
                target = g.last_seen
            if target and not (visible and d < 5):
                path = self.path_to(target, start=(g.x, g.y))
                if path:
                    waypoint = path[0]
                    dx, dy = waypoint[0] - g.x, waypoint[1] - g.y
                    length = math.hypot(dx, dy)
                    if length > 0.01:
                        g.angle = math.atan2(dy, dx)
                        self._move(g, dx / length, dy / length, min(1.5, length / DT))

    def _finish(self, outcome: str, oracle: str | None = None) -> None:
        if self.outcome is not None:
            return
        self.outcome, self.oracle = outcome, oracle
        if oracle:
            self.emit("oracle", oracle=oracle, x=self.player.x, y=self.player.y)
        self.emit("run_end", outcome=outcome, oracle=oracle, x=self.player.x, y=self.player.y,
                  distance_walked=self.distance_walked,
                  counts={name: self.counts[name] for name in
                          ("shots", "hits", "kills", "contacts", "heals", "loot", "damage_taken",
                           "explosions", "unwarned_explosion_deaths", "loot_opportunities")},
                  extraction_attempts=self.extraction_attempts,
                  extract_zone_seconds=self.extract_zone_steps / HZ,
                  in_extraction_zone=distance(self.position, self.exit_pos) < 1.0,
                  hp=self.player.hp,
                  loot=self.player.loot)

    def _numeric_ok(self) -> bool:
        p = self.player
        return (all(math.isfinite(value) for value in
                    (p.x, p.y, p.hp, p.max_hp, p.ammo, p.reserve, p.heals,
                     p.stamina, p.angle, p.loot))
                and p.max_hp > 0 and 0 <= p.hp <= p.max_hp
                and 0 <= p.ammo <= 8 and p.reserve >= 0 and p.heals >= 0
                and 0 <= p.stamina <= 100
                and all(all(math.isfinite(value) for value in
                            (g.x, g.y, g.hp, g.angle)) and 0 <= g.hp <= 60
                        for g in self.guards))

    def _oracles(self, previous, attempted: bool) -> None:
        p = self.player
        if not self._numeric_ok():
            self._finish("oracle", "numeric")
        elif distance(previous, self.position) > 2:
            self._finish("oracle", "teleport")
        elif not 0 <= p.x <= self.width or not 0 <= p.y <= self.height:
            self._finish("oracle", "out_of_world")
        elif any(not math.isfinite(g.x) or not math.isfinite(g.y) or not 0 <= g.x <= self.width or not 0 <= g.y <= self.height for g in self.guards):
            self._finish("oracle", "enemy_out_of_world")
        elif self._door_without_key:
            self._finish("oracle", "door_without_key")
        elif any(rect[0] < p.x < rect[2] and rect[1] < p.y < rect[3] for rect in self.solids()):
            self._finish("oracle", "solid_collision")
        if self.outcome:
            return
        # Inspect simulation time, including brief pauses while the executor
        # replans. Clearing on one idle tick would hide a trapped movement
        # tactic forever when it expires and restarts every few seconds.
        self._movement.append((self.step, self.distance_walked, attempted))
        if (len(self._movement) == 601 and self.step - self._movement[0][0] >= 600
                and sum(item[2] for item in list(self._movement)[1:]) >= 480
                and self.distance_walked - self._movement[0][1] < 0.5):
            self._finish("oracle", "stuck")
        cell = (int(p.x / 3), int(p.y / 3))
        if cell not in self._visited:
            self._visited.add(cell)
            self._last_new_cell = self.step
            self._warned_progress = False
        if not self._warned_progress and self.step - max(self._last_new_cell, self._last_contact) >= 120 * HZ:
            self.emit("warning", oracle="no_progress")
            self._warned_progress = True

    def tick(self, inputs: dict) -> None:
        """Advance a tick with move/aim and action buttons, recording only game events."""
        if self.outcome is not None:
            return
        self.step += 1
        if not self._numeric_ok():
            self._finish("oracle", "numeric")
            return
        p = self.player
        previous = self.position
        p.stance = inputs.get("stance", "stand")
        if "aim" in inputs:
            aim = inputs["aim"]
            if distance(self.position, aim) > 1e-9:
                p.angle = math.atan2(aim[1] - p.y, aim[0] - p.x)
        dx, dy = inputs.get("move", (0.0, 0.0))
        attempted = math.hypot(dx, dy) > 1e-8
        speed = self.tuning["player.speed"]
        sprint = inputs.get("sprint", False) and p.stamina > 0 and p.stance != "crouch"
        speed *= 1.6 if sprint else 0.55 if p.stance == "crouch" else 1.0
        if self._heal_until:
            speed *= 0.25
        p.stamina = max(0.0, p.stamina - 12 * DT) if sprint and attempted else min(100.0, p.stamina + 8 * DT)
        if self.bug == "corner_pocket" and distance(self.position, (10.6, 6.6)) < 0.65:
            self._trapped = True
        if not self._trapped:
            self._move(p, dx, dy, speed, ignore_walls=self.bug == "thin_wall" and sprint)
        moved = distance(previous, self.position)
        self.distance_walked += moved
        if moved > 1e-8 and p.stance != "crouch" and self.step % 15 == 0:
            self._sound("footsteps", p.x, p.y)
        if distance(self.position, self.exit_pos) < 1.0:
            self.extract_zone_steps += 1
            if inputs.get("extract"):
                self.extraction_attempts += 1
        for name, crate in self.crates.items():
            if (name not in self._loot_opportunities and not crate["looted"]
                    and distance(self.position, (crate["x"], crate["y"])) < 1.2):
                self._loot_opportunities.add(name)
                self.counts["loot_opportunities"] += 1
        self._player_actions(inputs)
        self._oracles(previous, attempted)
        # Terminal success cannot hide an invariant violation from this tick.
        if not self.outcome and self._extract_steps >= HZ:
            self.emit("extract", loot=p.loot)
            self._finish("extract")
        if not self.outcome:
            self._guards_tick(inputs)
        for grenade in list(self.grenades):
            if not self.outcome and self.step >= grenade["explode_step"]:
                self.grenades.remove(grenade)
                self.counts["explosions"] += 1
                self.emit("explosion", x=grenade["x"], y=grenade["y"], warned=grenade["warned"])
                if distance(self.position, (grenade["x"], grenade["y"])) < 3:
                    self._damage_player(100, "explosion", grenade["owner"], grenade["warned"])
        self.cues = [cue for cue in self.cues if self.step - cue["step"] <= 3 * HZ]
        if not self.outcome and self.step >= self.max_steps:
            self._finish("timeout")
