"""Sweep a max-time-to-resolution entry filter over real leader tapes.

Question this answers: the bot has no exit of its own — it mirrors the leader
or rides to resolution. A copy of a months-out macro market therefore parks
bankroll for months. Would refusing those entries have paid?

Read the census block before the sweep rows. The backtest can only score
markets that have RESOLVED (that is the only way to know the payout), but
long-horizon entries are precisely the ones still open, so the trades this
filter targets are the trades the backtest is least able to see. The census
counts what is missing so the sweep is not read as more than it is.

Horizon is measured from Gamma's `end_date`, the same stamp the live bot has
at entry time — not from a data-derived resolution. A filter scored on
information the live path lacks is not a filter you can ship.

Usage:
    python -m scripts.sweep_horizon --leaders 0xWALLET [0xWALLET ...]
    python -m scripts.sweep_horizon            # uses the bot's followed leaders
"""

from __future__ import annotations

import argparse
import os
import pickle
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.models import Side
from scripts._book import shared_book

# Ceilings in hours. None = filter off (today's behaviour).
HORIZONS = [None, 720.0, 336.0, 168.0, 72.0, 24.0, 12.0, 6.0]

CACHE = os.environ.get("PMBOT_TAPE_CACHE", ".tapes_horizon.pkl")

# Census buckets, in hours. Upper bound is exclusive of the next.
BUCKETS = [
    ("in-game (end_date past)", float("-inf"), 0.0),
    ("< 6h", 0.0, 6.0),
    ("6-24h", 6.0, 24.0),
    ("1-3d", 24.0, 72.0),
    ("3-7d", 72.0, 168.0),
    ("1-2wk", 168.0, 336.0),
    ("2-4wk", 336.0, 720.0),
    ("> 30d", 720.0, float("inf")),
]


def followed_leaders(db_path: str) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("select wallet from followed_leaders")]
    finally:
        con.close()


def load_tapes(bt, leaders: list[str], refresh: bool) -> dict:
    """Tapes are multi-MB and slow to page; cache so re-sweeps are free."""
    if not refresh:
        try:
            with open(CACHE, "rb") as fh:
                cached = pickle.load(fh)
            if set(cached) >= {w.lower() for w in leaders}:
                print(f"tapes: loaded {len(cached)} from {CACHE}", flush=True)
                return {w.lower(): cached[w.lower()] for w in leaders}
        except (OSError, ValueError, pickle.UnpicklingError):
            pass
    print(f"fetching tapes for {len(leaders)} leaders…", flush=True)
    tapes = bt.fetch_tapes(leaders)
    with open(CACHE + ".tmp", "wb") as fh:
        pickle.dump(tapes, fh)
    os.replace(CACHE + ".tmp", CACHE)
    return tapes


def census(bt, tapes: dict, *, now: datetime, lookback: int, s) -> None:
    """What the sweep can and cannot see, bucketed by entry horizon.

    Counts every BUY that clears the live entry gates (band + notional floor),
    then splits each horizon bucket into markets that have resolved — scorable
    — and markets still open, which the backtest silently drops.
    """
    cutoff = now - timedelta(days=lookback)
    rows = {label: {"resolved": 0, "open": 0} for label, _, _ in BUCKETS}
    unknown = no_date = 0
    for tape in tapes.values():
        for t in tape:
            if t.side is not Side.BUY or not t.token_id or not t.market_id:
                continue
            if not (cutoff <= t.timestamp <= now):
                continue
            if not (s.copy_price_min <= t.price <= s.copy_price_max):
                continue
            if t.usd_size < s.copy_min_leader_notional_usd:
                continue
            market, winner = bt._market(t.market_id)
            if market is None:
                unknown += 1
                continue
            if market.end_date is None:
                # No horizon to test against — the filter can never fire on it.
                no_date += 1
                continue
            hours = (market.end_date - t.timestamp).total_seconds() / 3600
            scorable = market.closed and winner is not None
            for label, lo, hi in BUCKETS:
                if lo <= hours < hi:
                    rows[label]["resolved" if scorable else "open"] += 1
                    break

    print("\n=== census: entries by horizon at trade time (Gamma end_date) ===")
    print("  the sweep can only score the 'resolved' column\n")
    print(f"  {'bucket':>24} {'resolved':>9} {'still open':>11} {'blind %':>8}")
    tot_r = tot_o = 0
    for label, _lo, _hi in BUCKETS:
        r, o = rows[label]["resolved"], rows[label]["open"]
        tot_r += r
        tot_o += o
        if r == 0 and o == 0:
            continue
        blind = 100 * o / (r + o)
        print(f"  {label:>24} {r:>9} {o:>11} {blind:>7.0f}%")
    total = tot_r + tot_o
    if total:
        print(f"  {'TOTAL':>24} {tot_r:>9} {tot_o:>11} {100*tot_o/total:>7.0f}%")
    if unknown:
        print(f"  ({unknown} entries dropped: market lookup failed)")
    if no_date:
        print(f"  ({no_date} entries have no end_date: no ceiling can fire on them)")


def sweep(bt, tapes, *, now, lookback, s, settings, label: str) -> list:
    print(f"\n=== {label} ===")
    print(f"  {'ceiling':>9} {'n':>4} {'invested':>11} {'net_pnl':>10} "
          f"{'roi%':>7} {'win%':>6} {'maxDD':>9}")
    rows = []
    for hours in HORIZONS:
        rep = bt.simulate(
            tapes, lookback_days=lookback, now=now, settings=settings,
            min_leader_notional=s.copy_min_leader_notional_usd,
            max_hours_to_resolution=hours or 0.0,
        )
        m = rep.metrics()
        rows.append((hours, m))
        tag = "off" if hours is None else (
            f"{hours/24:.0f}d" if hours >= 24 else f"{hours:.0f}h"
        )
        print(f"  {tag:>9} {m['n_trades']:>4} ${m['invested']:>10,.0f} "
              f"${m['net_pnl']:>9,.2f} {m['roi']*100:>6.1f} "
              f"{m['win_rate']*100:>5.1f} ${m['max_drawdown']:>8,.2f}", flush=True)
    return rows


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaders", nargs="*", default=None)
    ap.add_argument("--lookback", type=int, default=45)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--refresh", action="store_true", help="refetch tapes")
    # The local .env drifts from the VPS (the floor went 50 -> 150 on
    # 2026-08-02 there and not here), and the floor decides which horizons are
    # even in the population — so make it explicit rather than ambient.
    ap.add_argument("--floor", type=float, default=None,
                    help="min leader notional; defaults to settings")
    args = ap.parse_args(argv)

    s = get_settings()
    if args.floor is not None:
        s = s.model_copy(update={"copy_min_leader_notional_usd": args.floor})
    print(f"entry gates: band {s.copy_price_min}-{s.copy_price_max}, "
          f"leader notional floor ${s.copy_min_leader_notional_usd:.0f}")
    leaders = [w.lower() for w in (args.leaders or followed_leaders(s.db_path))]
    if not leaders:
        print("no leaders (pass --leaders, or run where the bot's db lives)")
        sys.exit(2)

    data, gamma = PolymarketDataClient(), GammaClient()
    bt = ExactCopyBacktester(data, gamma, trades_limit=args.limit)
    now = datetime.now(timezone.utc)

    t0 = time.time()
    tapes = load_tapes(bt, leaders, args.refresh)
    # Quote the book once for the whole tape: `simulate` prices every fill
    # off it and skips what it cannot quote, exactly as the live executor
    # does. Without this the run fills at the leader's own price.
    shared_book(bt, tapes)
    for w, tape in sorted(tapes.items()):
        print(f"  {w[:12]}… {len(tape)} trades", flush=True)
    print(f"({time.time()-t0:.0f}s)", flush=True)

    census(bt, tapes, now=now, lookback=args.lookback, s=s)

    sweep(bt, tapes, now=now, lookback=args.lookback, s=s, settings=None,
          label="capped (the bot's real $500 / $50-per-market limits)")

    # A ceiling frees bankroll and caps early, so tighter ceilings admit MORE
    # of the trades they did not block — the capped rows compare different
    # trade sets. Remove the caps and the only difference between rows is the
    # trades the ceiling itself removed.
    uncapped = s.model_copy(update={
        "bankroll_usd": 1e9, "max_per_market_usd": 1e9, "max_per_leader_usd": 1e9,
    })
    sweep(bt, tapes, now=now, lookback=args.lookback, s=s, settings=uncapped,
          label="uncapped (same trades in every row; isolates the filter)")

    data.close()
    gamma.close()


if __name__ == "__main__":
    main(sys.argv[1:])
