"""Game-owned visibility fixture: hidden maintenance state cannot reach a request."""

import json

from .adapter import Adapter
from .game import Game


def visibility_fixture():
    game = Game({"seed": 1, "map_name": "depot", "duration": 16, "profile": "courier",
                 "scenario": "standard", "tuning": {}})
    game.position = (1, 1)
    game.hidden_hazards = {(3, 1): 8001}
    before = Adapter(game, "rule").request()
    game.hidden_hazards = {(4, 3): 9001, (3, 3): 9002}
    after = Adapter(game, "rule").request()
    invisible_equal = before == after and before["state"]["seen"] == []
    game.position = (3, 3)
    visible = game.observation()["seen"]
    return {"passed": invisible_equal and len(visible) == 2,
            "behind_wall_not_seen": before["state"]["seen"] == [],
            "hidden_perturbation_invariant": invisible_equal, "visible_count": len(visible)}


if __name__ == "__main__":
    result = visibility_fixture()
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
