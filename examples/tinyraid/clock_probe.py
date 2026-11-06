"""A controlled host-clock fixture for the intentional wall-clock load fault.

Only this child process's monotonic clock is patched. The actual adapter,
stdio decision exchange, simulation and telemetry still execute normally.
"""

import argparse
from unittest.mock import patch

from . import __main__ as game_cli
from .adapter import Adapter


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--advance", type=float, default=0)
    args, remaining = parser.parse_known_args(argv)
    if args.advance not in (0, .2):
        parser.error("controlled advance must be 0 or 0.2")
    clock = [0.0]
    original = Adapter.decide

    def decide(self, state, reason):
        original(self, state, reason)
        clock[0] = args.advance

    with patch("time.monotonic", side_effect=lambda: clock[0]), patch.object(Adapter, "decide", decide):
        return game_cli.main(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
