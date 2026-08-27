"""Phase 3 smoke test: run auto-selection + one paper cycle against LIVE data.

Uses small API caps and relaxed filters so the full pipeline
(discover -> score -> select -> copy -> size -> paper-fill) actually exercises
end to end. Reads public data only; places no real orders.

Run: .venv/Scripts/python.exe scripts/verify_engine.py
"""

from __future__ import annotations

import logging

from pmbot.data import GammaClient, PolymarketDataClient, PriceCache
from pmbot.engine import Engine
from pmbot.execution import PaperExecutor
from pmbot.leaders.config import FilterConfig, LeaderConfig, SelectionConfig
from pmbot.leaders.scoring import LeaderSelector
from pmbot.portfolio.ledger import Ledger
from pmbot.risk import RiskManager
from pmbot.strategy import LongTermCopyStrategy

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    data = PolymarketDataClient()
    gamma = GammaClient()
    price_cache = PriceCache()
    ledger = Ledger(":memory:")

    # Relaxed config so the smoke test actually selects someone.
    cfg = LeaderConfig(
        selection=SelectionConfig(top_n=3, rescore_interval_hours=24),
        filters=FilterConfig(
            lookback_days=120, min_trades=5, min_resolved_markets=0,
            min_win_rate=0.0, min_realized_pnl_usd=-1e9,
            min_distinct_categories=1, max_profit_concentration=10.0,
            max_hours_since_last_trade=1e9, min_copyable_trades=0,
            min_tape_span_days=0.0,
        ),
    )
    selector = LeaderSelector(
        data, gamma, config=cfg,
        deep_score_limit=8, explore_n=0, feed_min_trades=1,
        trades_page=80, trades_cap=80,
        top_open_markets=3, top_closed_markets=2, per_market_trades=40,
        max_workers=2,
    )
    strategy = LongTermCopyStrategy(
        data, gamma, ledger,
        min_days_to_resolution=1, min_liquidity=1000, trades_per_leader=25,
    )
    engine = Engine(
        data=data, gamma=gamma, price_cache=price_cache, ledger=ledger,
        executor=PaperExecutor(ledger, price_cache),
        risk=RiskManager(ledger), selector=selector, strategy=strategy,
    )

    print("== Rescoring (live discovery + scoring) ==")
    engine.rescore()
    engine._print_leaders()

    if not engine.leaders:
        print("\nNo leaders selected (try widening caps). Pipeline ran OK.")
        engine.close()
        return 0

    print("\n== One paper cycle ==")
    fills, n = engine.poll_once()
    print(f"signals={n} paper_fills={fills}")

    print("\n== Resulting paper positions ==")
    for p in ledger.get_positions():
        print(f"  {p.outcome:>4s} {p.shares:>10.2f} sh @ {p.avg_price:.4f} "
              f"(${p.shares*p.avg_price:,.2f})  mkt={p.market_id[:12]}…")
    print(f"\nrealized=${ledger.realized_pnl_total():,.2f} fills={ledger.fill_count()}")
    print("\nOK: engine ran end to end in paper mode.")
    engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
