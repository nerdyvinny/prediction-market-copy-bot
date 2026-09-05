"""How much of the copy edge does our lag behind the leader eat?

The sim used to quote the book one POLL after the leader's print (10s). That
is not how late we are. The Data API's /trades feed is served through a 300s
CDN cache over an origin that publishes in ~200s batches, and 450 live fills
put our real end-to-end lag at a median of ~150s. This sweep replays the live
roster at a range of lags so the cost of that is a number rather than a worry.

    python -m scripts.sweep_lag [--days 30] [--lags 0,10,60,150,285,600]

The book is built ONCE at the loosest filters and shared across arms (see
scripts/_book.py): quote only one arm's needs and the others score zero
because "no quote" means "no trade".
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.leaders import load_leader_config
from scripts._book import shared_book

BANKROLL = 500.0
COPY_FRACTION = 0.10
MAX_PER_MARKET_PCT = 0.03
MAX_PER_LEADER = 400.0
MIN_LIQUIDITY = 5000.0
BAND = (0.15, 0.85)
MIN_LEADER_NOTIONAL = 150.0
SLIPPAGE_BPS = 60.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--lags", default="0,10,60,150,285,600",
                    help="total observation lags in seconds, comma separated")
    ap.add_argument("--leaders", default="",
                    help="comma-separated wallets (default: the live roster)")
    args = ap.parse_args()

    leaders = ([w.strip().lower() for w in args.leaders.split(",") if w.strip()]
               or [w.lower() for w in load_leader_config().roster])
    lags = [float(x) for x in args.lags.split(",")]

    s = get_settings().model_copy(update={
        "bankroll_usd": BANKROLL,
        "copy_fraction": COPY_FRACTION,
        "max_per_market_pct": MAX_PER_MARKET_PCT,
        "max_per_leader_usd": MAX_PER_LEADER,
        "min_market_liquidity_usd": MIN_LIQUIDITY,
        "compound_profits": False,
        "copy_price_min": BAND[0],
        "copy_price_max": BAND[1],
        "copy_min_leader_notional_usd": MIN_LEADER_NOTIONAL,
        "slippage_bps": SLIPPAGE_BPS,
    })
    now = datetime.now(timezone.utc)
    bt = ExactCopyBacktester(PolymarketDataClient(), GammaClient(), s,
                             slippage_bps=SLIPPAGE_BPS, trades_limit=3000)
    print(f"roster ({len(leaders)}): {', '.join(w[:10] for w in leaders)}")
    print(f"window: {now - timedelta(days=args.days):%Y-%m-%d} .. {now:%Y-%m-%d}\n")
    tapes = bt.fetch_tapes(leaders)
    print(f"tapes: {sum(len(v) for v in tapes.values())} trades", flush=True)
    # Narrowed to the scored window on purpose. `shared_book` defaults to the
    # whole tape span (265 days on the longest roster wallet), which is right
    # for a walk-forward but pure waste here: every arm scores the same 30 days
    # at the same filters and differs only in lag, and the fetch window already
    # trails each moment by 900s, so the longest lag is still quoted.
    shared_book(bt, tapes, now=now, lookback_days=args.days + 1)

    print(f"\n{'lag':>7} {'trades':>7} {'deployed':>11} {'net':>10} {'ROI/dep':>9} "
          f"{'win':>7} {'drift':>7} {'limit':>7} {'nobook':>7} {'caps':>6}")
    print("-" * 86)
    for lag in lags:
        rep = bt.simulate(
            tapes, lookback_days=args.days, now=now,
            min_leader_notional=MIN_LEADER_NOTIONAL,
            price_min=BAND[0], price_max=BAND[1], settings=s,
            slippage_bps=SLIPPAGE_BPS, skip_round_tripped_entries=True,
            entry_lag_seconds=lag,
        )
        m = rep.metrics()
        sk = rep.skipped or {}
        print(f"{lag:>6.0f}s {m['n_trades']:>7} {m['invested']:>10,.0f} "
              f"{m['net_pnl']:>+10,.2f} {m['roi']*100:>8.2f}% {m['win_rate']*100:>6.1f}% "
              f"{sk.get('price_drift',0):>7} {sk.get('entry_limit',0):>7} "
              f"{sk.get('no_book',0):>7} {sk.get('bankroll_or_caps',0):>6}")
    print("\nlag 150s is the measured live figure (scripts/measure_copy_lag.py);")
    print("lag 10s is the poll interval alone, i.e. what every earlier run assumed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
