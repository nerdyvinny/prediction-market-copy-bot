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

    bt = sub.add_parser("backtest", help="replay historical leader trades")
    bt.add_argument("--leaders", type=str, default=None,
                    help="comma-separated wallets to backtest (default: auto-select)")
    bt.add_argument("--lookback", type=int, default=180, help="days of history (default 180)")
    bt.add_argument("--limit", type=int, default=500, help="max trades per leader (default 500)")

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
        from pmbot.backtest import Backtester
        from pmbot.data import GammaClient, PolymarketDataClient
        from pmbot.leaders.scoring import LeaderSelector

        data, gamma = PolymarketDataClient(), GammaClient()
        try:
            if args.leaders:
                leaders = [w.strip().lower() for w in args.leaders.split(",") if w.strip()]
            else:
                print("· auto-selecting leaders to backtest…")
                leaders = [r.wallet for r in LeaderSelector(data, gamma).select()]
            if not leaders:
                print("No leaders to backtest (loosen leaders.yaml or pass --leaders).")
                return 0
            print(f"· backtesting {len(leaders)} leader(s) over {args.lookback}d…\n")
            report = Backtester(data, gamma, settings,
                                lookback_days=args.lookback, trades_limit=args.limit).run(leaders)
            print(report.summary_text())
        finally:
            data.close()
            gamma.close()
        return 0

    if args.mode == "live":
        print("Refusing to run live: live execution is gated until Phase 6.")
        print(_COMPLIANCE)
        return 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
