"""What should the entry drift budget be, now that we know how late we are?

`copy_max_price_drift` refuses an entry whose quote has moved further than the
budget from the leader's fill. At the 10-second lag the sim used to assume,
that read as an execution guard. At the ~150s lag we actually run at
(`copy_feed_lag_seconds`), it is really a filter on "did this price move in two
and a half minutes" — and the lag sweep showed it, not the fill price, is where
the lag gets paid: 26 refusals at 10s, 71 at 150s, with ROI barely moving and
turnover down 17%.

So the question is a real one: pay for more trades with worse fills, or keep
the budget tight and deploy less of a bankroll that is the binding constraint.

Three non-overlapping 30-day windows, because one window's argmax is not a
finding — the shape the curve keeps across windows is.

    python -m scripts.sweep_drift [--windows 3] [--span 30]
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
BAND = (0.15, 0.85)
MIN_LEADER_NOTIONAL = 150.0
SLIPPAGE_BPS = 60.0
DRIFTS = [0.01, 0.02, 0.03, 0.05, 0.08, 0.15, 0.0]   # 0.0 = guard off


def peak_capital(results) -> float:
    """Most money open at one instant. The backtest's `deployed` is turnover,
    not exposure, and an arm that only wins by holding 3x the bankroll at once
    is not an arm we can run — see docs and the $500 constraint."""
    events = []
    for r in results:
        events.append((r.entry_ts, r.size_usd))
        events.append((max(r.resolve_ts, r.entry_ts), -r.size_usd))
    events.sort(key=lambda e: e[0])
    cur = peak = 0.0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return peak


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=3)
    ap.add_argument("--span", type=int, default=30)
    ap.add_argument("--leaders", default="")
    args = ap.parse_args()

    leaders = ([w.strip().lower() for w in args.leaders.split(",") if w.strip()]
               or [w.lower() for w in load_leader_config().roster])
    s = get_settings().model_copy(update={
        "bankroll_usd": BANKROLL, "copy_fraction": 0.10, "max_per_market_pct": 0.03,
        "max_per_leader_usd": 400.0, "min_market_liquidity_usd": 5000.0,
        "compound_profits": False, "copy_price_min": BAND[0], "copy_price_max": BAND[1],
        "copy_min_leader_notional_usd": MIN_LEADER_NOTIONAL, "slippage_bps": SLIPPAGE_BPS,
    })
    now = datetime.now(timezone.utc)
    lag = s.copy_feed_lag_seconds + s.poll_interval_seconds
    bt = ExactCopyBacktester(PolymarketDataClient(), GammaClient(), s,
                             slippage_bps=SLIPPAGE_BPS, trades_limit=3000)
    print(f"roster ({len(leaders)}): {', '.join(w[:10] for w in leaders)}")
    print(f"copy lag {lag:.0f}s (the measured one) | {args.windows} x {args.span}d windows\n")
    tapes = bt.fetch_tapes(leaders)
    print(f"tapes: {sum(len(v) for v in tapes.values())} trades", flush=True)
    shared_book(bt, tapes, now=now, lookback_days=args.windows * args.span + 1)

    for w in range(args.windows):
        end = now - timedelta(days=w * args.span)
        print(f"\n=== window {w+1}: {end - timedelta(days=args.span):%Y-%m-%d} .. {end:%Y-%m-%d} ===")
        print(f"{'drift':>7} {'trades':>7} {'deployed':>10} {'net':>10} {'ROI/dep':>9} "
              f"{'win':>7} {'peak cap':>10} {'maxDD':>8} {'drift-skip':>11}")
        print("-" * 88)
        for d in DRIFTS:
            rep = bt.simulate(
                tapes, lookback_days=args.span, now=end,
                min_leader_notional=MIN_LEADER_NOTIONAL,
                price_min=BAND[0], price_max=BAND[1], settings=s,
                slippage_bps=SLIPPAGE_BPS, skip_round_tripped_entries=True,
                max_price_drift=d,
            )
            m = rep.metrics()
            sk = rep.skipped or {}
            label = "off" if d == 0 else f"{d:.2f}"
            print(f"{label:>7} {m['n_trades']:>7} {m['invested']:>9,.0f} "
                  f"{m['net_pnl']:>+10,.2f} {m['roi']*100:>8.2f}% {m['win_rate']*100:>6.1f}% "
                  f"{peak_capital(rep.results):>9,.0f} {m['max_drawdown']:>8,.0f} "
                  f"{sk.get('price_drift',0):>11}")
    print(f"\nlive setting is 0.03. Peak cap above ${BANKROLL:,.0f} means the arm spends "
          f"money\nthe bot does not have, so read it before reading the P&L.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
