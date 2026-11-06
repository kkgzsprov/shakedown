"""Deterministic puzzle rules; every player and adapter action uses ``tick``."""

from __future__ import annotations

import math
import random


MOVES = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
MAPS = {"depot": {(2, 1), (2, 2)}, "loop": {(2, 1), (3, 1)}}
PROFILES = ("courier", "careful")


def validate_config(config):
    if type(config) is not dict or set(config) != {"seed", "map_name", "duration", "profile", "scenario", "tuning"}:
        raise ValueError("invalid switchyard startup configuration")
    if type(config["seed"]) is not int or config["map_name"] not in MAPS:
        raise ValueError("invalid seed or map")
    duration = config["duration"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 256:
        raise ValueError("duration must be in (0, 256] turns")
    if config["profile"] not in PROFILES or config["scenario"] not in {"standard", "stuck"}:
        raise ValueError("invalid profile or scenario")
    tuning = config["tuning"]
    if type(tuning) is not dict or set(tuning) - {"cargo.value"}:
        raise ValueError("unknown tuning key")
    if type(tuning.get("cargo.value", 1)) is not int or not 1 <= tuning.get("cargo.value", 1) <= 3:
        raise ValueError("cargo.value must be an integer in [1, 3]")


def validate_input(value):
    if (type(value) is not dict or set(value) != {"button"}
            or type(value["button"]) is not str or value["button"] not in {*MOVES, "collect", "deliver", "wait"}):
        raise ValueError("input must contain one known button")
    return value


class Game:
    width, height = 5, 4
    depot, dock = (1, 0), (4, 3)

    def __init__(self, config):
        validate_config(config)
        self.config = dict(config)
        self.seed = config["seed"]
        self.position = (0, 0)
        self.walls = set(MAPS[config["map_name"]])
        if config["scenario"] == "stuck":
            self.walls.update({(1, 0), (0, 1)})
        # Maintenance traffic is not public until it is next to the courier.
        self.hidden_hazards = {(4, 0): random.Random(self.seed).randrange(1000, 9999)}
        self.step = self.distance = self.cargo = self.loot = self.idle = 0
        self.outcome = self.oracle = None
        self.events = []
        self.emit("run_begin", seed=self.seed, map=config["map_name"], game="switchyard")

    @property
    def time(self):
        return float(self.step)

    def emit(self, kind, **fields):
        self.events.append({"type": kind, "step": self.step, "t": self.time, **fields})

    def legal_buttons(self):
        result = ["wait"]
        for button, (dx, dy) in MOVES.items():
            destination = (self.position[0] + dx, self.position[1] + dy)
            if (0 <= destination[0] < self.width and 0 <= destination[1] < self.height
                    and destination not in self.walls):
                result.append(button)
        if self.position == self.depot and not self.cargo:
            result.append("collect")
        if self.position == self.dock and self.cargo:
            result.append("deliver")
        return result

    def observation(self):
        seen = [{"id": f"maintenance-{position[0]}-{position[1]}", "position": list(position)}
                for position in sorted(self.hidden_hazards)
                if self.visible(position)]
        return {"self": {"position": list(self.position), "cargo": self.cargo},
                "seen": seen, "public_map": {"size": [self.width, self.height],
                    "walls": [list(position) for position in sorted(self.walls)]},
                "objective": {"depot": list(self.depot), "dock": list(self.dock),
                              "turns_left": max(0, math.ceil(self.config["duration"]) - self.step)}}

    def visible(self, position):
        if position in self.walls:
            return False
        dx, dy = position[0] - self.position[0], position[1] - self.position[1]
        distance = abs(dx) + abs(dy)
        if distance > 2 or (dx and dy):
            return False
        direction = ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))
        return all((self.position[0] + direction[0] * offset,
                    self.position[1] + direction[1] * offset) not in self.walls
                   for offset in range(1, distance))

    def tick(self, event_input):
        if self.outcome is not None:
            raise ValueError("input after game end")
        button = validate_input(event_input)["button"]
        if button not in self.legal_buttons():
            raise ValueError("button is not currently legal")
        previous = self.position
        if button in MOVES:
            dx, dy = MOVES[button]
            self.position = (previous[0] + dx, previous[1] + dy)
            self.distance += 1
            self.emit("move", button=button, x=self.position[0], y=self.position[1])
            if self.position in self.hidden_hazards:
                self.emit("contact", id="maintenance", initiated_by="enemy")
                self.emit("damage_taken", amount=1, cause="maintenance")
                self.outcome = "death"
                self.emit("death", cause="maintenance")
        elif button == "collect":
            self.cargo = 1
            self.loot = self.config["tuning"].get("cargo.value", 1)
            self.emit("loot", amount=self.loot)
        elif button == "deliver":
            self.outcome = "extract"
            self.emit("extract", cargo=self.cargo)
        self.idle = self.idle + 1 if self.position == previous else 0
        self.step += 1
        if self.config["scenario"] == "stuck" and self.idle >= 4:
            self.oracle, self.outcome = "stuck", "oracle"
            self.emit("oracle", oracle="stuck")
        if self.outcome is None and self.step >= self.config["duration"]:
            self.outcome = "timeout"
        if self.outcome is not None:
            self.emit("run_end", outcome=self.outcome, oracle=self.oracle,
                      x=self.position[0], y=self.position[1], distance_walked=self.distance,
                      loot=self.loot, counts={"loot": self.loot, "shots": 0, "heals": 0})
