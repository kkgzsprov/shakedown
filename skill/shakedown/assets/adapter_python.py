"""Executable Python transport skeleton; game input/replay/visibility remain game-owned.

The command-line demo performs one bridge decision only. It is not a game and
does not claim conformance. Import WireSession into an actual fixed-step adapter.
"""

from __future__ import annotations

import argparse
import sys

from shakedown.deciders.tape import TapeDecider, TapeDivergence
from shakedown.protocol import PREFIX, ProtocolError, canonical_json, parse_answer, parse_game_line


class WireSession:
    def __init__(self, *, seed, profile, game, adapter_version="1", mode="bridge",
                 reader=None, writer=None, rule=None):
        self.seed, self.profile, self.game = seed, profile, game
        self.adapter_version, self.mode = adapter_version, mode
        self.reader = sys.stdin if reader is None else reader
        self.writer = sys.stdout if writer is None else writer
        self.rule = rule
        self.tape = TapeDecider(mode[5:]) if mode.startswith("tape:") else None
        if mode not in {"bridge", "rule"} and self.tape is None:
            raise ProtocolError("unsupported adapter mode")
        self.seq, self.started, self.ended = 0, False, False

    def _emit(self, message):
        line = PREFIX + canonical_json(message).decode("utf-8") + "\n"
        parse_game_line(line)
        self.writer.write(line)
        self.writer.flush()

    def start(self):
        if self.started:
            raise ProtocolError("hello must occur once")
        hello = {"v": 1, "type": "hello", "game": self.game,
                 "adapter_version": self.adapter_version, "seed": self.seed,
                 "profile": self.profile}
        parse_game_line(PREFIX + canonical_json(hello).decode("utf-8"))
        if self.mode == "bridge":
            self._emit(hello)
        self.started = True

    def choose(self, *, step, state, candidates, order_seed, reason):
        if not self.started or self.ended:
            raise ProtocolError("decision outside an active session")
        self.seq += 1
        request = {"v": 1, "type": "decide", "seq": self.seq, "step": step,
                   "reason": reason, "profile": self.profile, "state": state,
                   "candidates": candidates, "order_seed": order_seed}
        parse_game_line(PREFIX + canonical_json(request).decode("utf-8"))
        if self.mode == "bridge":
            self._emit(request)
            choice = parse_answer(self.reader.readline(), request)["choice"]
        elif self.tape is not None:
            choice = self.tape.decide(request, "").choice
        else:
            if self.rule is None:
                raise ProtocolError("rule mode requires the game's rule callback")
            choice = self.rule(request)
        if type(choice) is not str or choice not in candidates:
            raise ProtocolError("selected choice is not legal")
        return choice, self.seq

    def finish(self, *, outcome, step, oracle=None):
        if not self.started or self.ended:
            raise ProtocolError("end outside an active session")
        message = {"v": 1, "type": "end", "outcome": outcome, "step": step}
        if oracle is not None:
            message["oracle"] = oracle
        parse_game_line(PREFIX + canonical_json(message).decode("utf-8"))
        if self.mode == "bridge":
            self._emit(message)
        if self.tape is not None:
            self.tape.close()
        self.ended = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="bridge")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--profile", default="cautious")
    args = parser.parse_args(argv)
    try:
        session = WireSession(seed=args.seed, profile=args.profile, game="transport-demo",
                              mode=args.mode, rule=lambda request: "hold")
        session.start()
        session.choose(step=0, state={"self": {"hp": 100}, "seen": []},
                       candidates={"hold": "Keep the current action"}, order_seed=17, reason="timer")
        session.finish(outcome="timeout", step=1)
        return 0
    except TapeDivergence as error:
        print(str(error), file=sys.stderr)
        return 5
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
