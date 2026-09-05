"""Discover and walk-forward-vet NEW copyable leader wallets.

The engine's daily rescore only ever scores a slice of the harvested pool, so
the followed set may be far from the best available. This script attacks the
pool directly:

  1. HARVEST cheaply: pull the trade feeds of the top-volume open markets
     (one call per market, not per wallet) and profile every wallet seen —
     how many $500+ trades, across how many markets, how recently.
  2. PREFILTER to wallets that trade big, broadly, and recently.
  3. TUNE-VET: fetch each survivor's tape and exact-copy-backtest it on the
     TUNE window (45d ending 45d ago); rank by copy P&L.
  4. VALIDATE portfolios out-of-sample on the RECENT 45d: current leaders
     (flat vs conviction-weighted), current + best new wallets, and the
     best-12 combined — plus time-to-resolution entry-filter variants.
  5. Print a suggested allowlist for pmbot/config/leaders.yaml.

Usage:
    python -m scripts.discover_leaders 0xCURRENT [0xCURRENT ...]
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester, vet_weights
from pmbot.config import Settings
from pmbot.data import GammaClient, PolymarketDataClient
from scripts._book import shared_book

WINDOW_DAYS = 45
MIN_NOTIONAL = 500.0
BAND = (0.15, 0.85)

HARVEST_MARKETS = 60          # top-volume open markets to profile
FEED_LIMIT = 500              # trades pulled per market feed
BIG_TRADE_USD = 500.0         # "whale print" threshold when profiling feeds
MIN_BIG_TRADES = 2            # prefilter: at least this many whale prints...
MIN_MARKETS = 2               # ...spread over at least this many markets
MAX_VET = 120                 # cap on wallets that get a full tape + backtest
TUNE_MIN_TRADES = 3           # tune-window evidence floor to call a wallet "good"
TUNE_MIN_ROI = 0.05
TOP_NEW = 12


def _settings():
    return Settings(bankroll_usd=500.0, copy_fraction=0.10, max_per_market_usd=100.0,
                    max_per_leader_usd=400.0, compound_profits=False)


def main(current: list[str]) -> None:
    now = datetime.now(timezone.utc)
    tune_now = now - timedelta(days=WINDOW_DAYS)
    chosen = _settings()
    data, gamma = PolymarketDataClient(), GammaClient()
    # 2000-trade tapes: heavy traders burn 1000 trades in ~2 weeks, which
    # doesn't reach the tune window and silently zeroes their comparison.
    bt = ExactCopyBacktester(data, gamma, settings=chosen, trades_limit=2000)
    t0 = time.time()

    def sim(tapes, when, weights=None, min_hours=0.0):
        return bt.simulate(tapes, lookback_days=WINDOW_DAYS, min_leader_notional=MIN_NOTIONAL,
                           price_min=BAND[0], price_max=BAND[1], now=when,
                           leader_weights=weights, min_hours_to_resolution=min_hours).metrics()

    # --- 1) harvest wallet profiles from top-market trade feeds -----------
    print(f"profiling wallets from top {HARVEST_MARKETS} market feeds…", flush=True)
    stats = defaultdict(lambda: {"big": 0, "mkts": set(), "last": None})
    try:
        markets = gamma.get_markets(limit=HARVEST_MARKETS)
    except Exception as e:
        print(f"market listing failed: {e}")
        sys.exit(1)
    for m in markets:
        if not m.market_id:
            continue
        try:
            feed = data.get_trades(market=m.market_id, limit=FEED_LIMIT)
        except Exception:
            continue
        for t in feed:
            if not t.leader:
                continue
            st = stats[t.leader.lower()]
            st["mkts"].add(t.market_id)
            if t.usd_size >= BIG_TRADE_USD:
                st["big"] += 1
            if st["last"] is None or t.timestamp > st["last"]:
                st["last"] = t.timestamp
    print(f"  profiled {len(stats)} wallets in {time.time()-t0:.0f}s", flush=True)

    # --- 2) prefilter ------------------------------------------------------
    cur = {w.lower() for w in current}
    cands = [
        (w, st["big"]) for w, st in stats.items()
        if w not in cur and st["big"] >= MIN_BIG_TRADES and len(st["mkts"]) >= MIN_MARKETS
    ]
    cands.sort(key=lambda x: -x[1])
    cands = [w for w, _ in cands[:MAX_VET]]
    print(f"  {len(cands)} candidates pass prefilter (vetting top {len(cands)})", flush=True)

    # --- 3) tune-window vet of candidates + current leaders ----------------
    print("fetching tapes + tune-window backtests…", flush=True)
    tapes = bt.fetch_tapes(list(cur) + cands)
    # Quote the book once for the whole tape: `simulate` prices every fill
    # off it and skips what it cannot quote, exactly as the live executor
    # does. Without this the run fills at the leader's own price.
    shared_book(bt, tapes)
    tune_pnl: dict[str, tuple[float, float, int]] = {}   # wallet -> (pnl, roi, n)
    for i, w in enumerate(tapes, 1):
        m = sim({w: tapes[w]}, tune_now)
        roi = m["roi"]
        tune_pnl[w] = (m["net_pnl"], roi, m["n_trades"])
        if i % 20 == 0:
            print(f"  …{i}/{len(tapes)} vetted ({time.time()-t0:.0f}s)", flush=True)

    good_new = [
        w for w in cands
        if w in tune_pnl and tune_pnl[w][2] >= TUNE_MIN_TRADES
        and tune_pnl[w][0] > 0 and tune_pnl[w][1] >= TUNE_MIN_ROI
    ]
    good_new.sort(key=lambda w: -tune_pnl[w][0])
    good_new = good_new[:TOP_NEW]

    print(f"\n=== tune-window results — top new wallets ({len(good_new)}) ===")
    for w in good_new:
        pnl, roi, n = tune_pnl[w]
        print(f"  {w}  n={n:>3} pnl=${pnl:>8,.2f} roi={roi*100:>5.1f}%")
    print("=== current leaders on tune window ===")
    for w in sorted(cur, key=lambda w: -tune_pnl.get(w, (0, 0, 0))[0]):
        pnl, roi, n = tune_pnl.get(w, (0.0, 0.0, 0))
        print(f"  {w}  n={n:>3} pnl=${pnl:>8,.2f} roi={roi*100:>5.1f}%")

    # --- 4) out-of-sample portfolio validation ----------------------------
    def weights_for(members):
        rois = {w: tune_pnl[w][1] for w in members
                if w in tune_pnl and tune_pnl[w][2] >= TUNE_MIN_TRADES and tune_pnl[w][1] > 0}
        return vet_weights(rois)

    combined = sorted(set(cur) | set(good_new),
                      key=lambda w: -tune_pnl.get(w, (0, 0, 0))[0])
    portfolios = {
        "current-8 flat": (cur, None, 0.0),
        "current-8 weighted": (cur, weights_for(cur), 0.0),
        f"current+top4new weighted": (set(cur) | set(good_new[:4]), None, 0.0),
        "best-12 combined weighted": (set(combined[:12]), None, 0.0),
        "best-12 + 6h-to-close": (set(combined[:12]), None, 6.0),
        "best-12 + 24h-to-close": (set(combined[:12]), None, 24.0),
    }
    print(f"\n=== VALIDATE window (last {WINDOW_DAYS}d) — portfolios ===")
    for name, (members, w8s, min_h) in portfolios.items():
        if w8s is None and "flat" not in name:
            w8s = weights_for(members)
        sub = {w: tapes[w] for w in members if w in tapes}
        m = sim(sub, now, weights=w8s if "flat" not in name else None, min_hours=min_h)
        print(f"  {name:<28} n={m['n_trades']:>4} net=${m['net_pnl']:>9,.2f} "
              f"roi={m['roi']*100:>5.1f}% mdd=${m['max_drawdown']:>7,.2f}")

    # --- 5) suggested allowlist -------------------------------------------
    print("\nsuggested leaders.yaml allowlist additions:")
    for w in good_new:
        print(f"  - {w}")
    print(f"\n(total {time.time()-t0:.0f}s)")
    data.close()
    gamma.close()


if __name__ == "__main__":
    main([w.lower() for w in sys.argv[1:]])
