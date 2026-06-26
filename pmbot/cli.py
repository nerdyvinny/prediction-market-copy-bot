"""Command-line entrypoint.

Usage:
    pmbot paper       # run the copy-trading loop in simulation (no real orders)
    pmbot backtest    # replay historical leader trades (Phase 4)
    pmbot live        # GATED — refuses unless explicitly, lawfully enabled

This is an early stub; the paper/backtest loops are wired up in later phases.
"""

from __future__ import annotations

import argparse
import sys

from pmbot import __version__
from pmbot.config import RunMode, get_settings

_COMPLIANCE = (
    "Live trading is OFF by default. Only enable it where you are lawfully able "
    "to trade and fund the venue. This tool contains no geoblock/KYC bypass."
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pmbot", description="Prediction-market copy-trading bot.")
    p.add_argument("--version", action="version", version=f"pmbot {__version__}")
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("paper", help="run the loop in simulation (no real orders)")
    sub.add_parser("backtest", help="replay historical leader trades")
    sub.add_parser("live", help="GATED live trading (disabled by default)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    if args.mode == "paper":
        print(f"[pmbot {__version__}] paper mode - bankroll ${settings.bankroll_usd:.0f}")
        print("Engine not wired yet (Phase 3). Scaffolding is in place.")
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
