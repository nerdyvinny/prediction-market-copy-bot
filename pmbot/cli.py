"""Command-line entrypoint.

Usage:
    pmbot paper                 # run the copy-trading loop in simulation
    pmbot paper --cycles 1      # run a single cycle and exit
    pmbot backtest              # replay historical leader trades (Phase 4)
    pmbot live                  # GATED — refuses unless explicitly, lawfully enabled
"""

from __future__ import annotations

import argparse
import logging
import sys

from pmbot import __version__
from pmbot.config import get_settings

_COMPLIANCE = (
    "Live trading is OFF by default. Only enable it where you are lawfully able "
    "to trade and fund the venue. This tool contains no geoblock/KYC bypass."
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pmbot", description="Prediction-market copy-trading bot.")
    p.add_argument("--version", action="version", version=f"pmbot {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    sub = p.add_subparsers(dest="mode", required=True)

    paper = sub.add_parser("paper", help="run the loop in simulation (no real orders)")
    paper.add_argument("--cycles", type=int, default=None, help="run N cycles then exit (default: forever)")

    sub.add_parser("backtest", help="replay historical leader trades")
    sub.add_parser("live", help="GATED live trading (disabled by default)")
    return p


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    settings = get_settings()

    if args.mode == "paper":
        from pmbot.engine import Engine

        engine = Engine(settings=settings)
        try:
            engine.run(max_cycles=args.cycles)
        finally:
            engine.close()
        return 0

    if args.mode == "backtest":
        print(f"[pmbot {__version__}] backtest mode")
        print("Backtest harness arrives in Phase 4.")
        return 0

    if args.mode == "live":
        print("Refusing to run live: live execution is gated until Phase 6.")
        print(_COMPLIANCE)
        return 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
