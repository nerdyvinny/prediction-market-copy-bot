"""Phase 4 demo: backtest the engine on real RESOLVED markets.

Picks recently closed, high-volume markets and backtests every eligible BUY
entry in them (copying all participants), settling at the true outcome. This
validates the settlement/sizing/metrics math on real data and shows a market's
copyable edge. Reads public data only.

Run: .venv/Scripts/python.exe scripts/run_backtest_demo.py
"""

from __future__ import annotations

import logging

from pmbot.backtest import Backtester
from pmbot.data import GammaClient, PolymarketDataClient

logging.basicConfig(level=logging.WARNING)


def main() -> int:
    data = PolymarketDataClient()
    gamma = GammaClient()
    try:
        print("· finding recently closed, high-volume markets…")
        closed = gamma.get_markets(limit=12, closed=True, active=None, order="volume")
        cids = [m.market_id for m in closed if m.market_id]
        print(f"· backtesting entries across {len(cids)} resolved markets…\n")

        # min_days=0 so short-horizon resolved markets are included for the demo.
        report = Backtester(
            data, gamma, trades_limit=120, min_days_to_resolution=0,
        ).run_on_markets(cids)
        print(report.summary_text())
    finally:
        data.close()
        gamma.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
