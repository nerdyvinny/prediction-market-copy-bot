"""Is holding a copy to resolution profitable, or is all the edge in leader exits?

Two live settlements (2026-07-28 and 07-30) cost -$148.95 against +$116 from
everything else, and in BOTH the leader rode to zero alongside us — so there is
no missed-exit bug to fix. The open question is risk, not correctness: we take
the leaders' full binary tail whenever they hold, and we want to know whether
that bucket pays for itself.

`ExactCopyBacktester` is the right instrument for exactly this question, even
though it is the wrong one for the notional floor (pmb-copy-params): its blind
spot is round-trip FILL QUALITY, and a settlement payout is an exact 0 or 1 that
does not depend on fills at all. So the `resolution` bucket below is modelled
faithfully; treat the `leader-exit` bucket as the optimistic bound it is.

Reports the same split under a few candidate mitigations so the choice is made
on out-of-sample numbers rather than on the two losses that prompted it.

Usage:
    python -m scripts.split_settlement                 # uses followed_leaders
    python -m scripts.split_settlement --days 90
"""

from __future__ import annotations

import argparse
import os
import pickle
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from scripts._book import shared_book

BAND = (0.15, 0.85)
CACHE = ".tapes_split.pkl"


def followed_leaders(db_path: str) -> list[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("select wallet from followed_leaders")]
    finally:
        con.close()


def buckets(rep):
    """Split a report's tranches by how they ended."""
    out = defaultdict(list)
    for r in rep.results:
        out[r.closed_by].append(r)
    return out


def show(rep, label):
    b = buckets(rep)
    total = sum(r.pnl for r in rep.results)
    print(f"\n=== {label} ===")
    print(f"{'closed_by':14s} {'n':>5s} {'invested':>11s} {'net':>10s} "
          f"{'roi%':>7s} {'win%':>6s} {'worst':>9s}")
    for k in ("leader-exit", "resolution", "stop-loss"):
        rs = b.get(k, [])
        if not rs:
            continue
        inv = sum(r.size_usd for r in rs)
        net = sum(r.pnl for r in rs)
        wins = sum(1 for r in rs if r.pnl > 0)
        worst = min((r.pnl for r in rs), default=0.0)
        print(f"{k:14s} {len(rs):5d} ${inv:10,.0f} ${net:9,.0f} "
              f"{(net/inv*100 if inv else 0):6.1f} {wins/len(rs)*100:5.1f} ${worst:8,.0f}")
    print(f"{'TOTAL':14s} {len(rep.results):5d} "
          f"${sum(r.size_usd for r in rep.results):10,.0f} ${total:9,.0f}")
    return b


def by_leader(rep):
    """Per-leader resolution-bucket P&L — is the tail concentrated?"""
    agg = defaultdict(lambda: [0, 0.0, 0.0, 0])   # n, invested, pnl, wins
    for r in rep.results:
        if r.closed_by != "resolution":
            continue
        a = agg[r.leader]
        a[0] += 1
        a[1] += r.size_usd
        a[2] += r.pnl
        a[3] += 1 if r.pnl > 0 else 0
    print(f"\n=== resolution bucket, per leader ===")
    print(f"{'leader':16s} {'n':>5s} {'invested':>11s} {'net':>10s} {'win%':>6s}")
    for w, (n, inv, pnl, wins) in sorted(agg.items(), key=lambda x: x[1][2]):
        print(f"{w[:16]:16s} {n:5d} ${inv:10,.0f} ${pnl:9,.0f} {wins/n*100:5.1f}")


def windows(rep, days, n_win=6):
    """Per-window bucket P&L — is 'resolution is profitable' stable, or is it
    one lucky stretch? A single span's aggregate cannot tell those apart, and
    the whole reason we are here is that two losses in one week looked like a
    trend (pmb-copy-params: read the curve across windows, not the argmax).
    """
    if not rep.results:
        return
    # Window by ENTRY time, not resolve_ts: daily-sports markets stamp
    # end_date at the start of the day, so resolve_ts is routinely earlier than
    # the entry and sorting on it scrambles the timeline (it made this table
    # come back empty). entry_ts is always the real instant we would have filled.
    end = max(r.entry_ts for r in rep.results)
    start = end - timedelta(days=days)
    width = (end - start) / n_win
    print(f"\n=== per-window ({n_win} x {width.days}d), resolution bucket ===")
    print(f"{'window':14s} {'n':>5s} {'net':>9s} {'win%':>6s}   {'leader-exit net':>15s}")
    neg = 0
    for i in range(n_win):
        a, b = start + i * width, start + (i + 1) * width
        res = [r for r in rep.results if a <= r.entry_ts < b and r.closed_by == "resolution"]
        exi = [r for r in rep.results if a <= r.entry_ts < b and r.closed_by == "leader-exit"]
        if not res:
            continue
        net = sum(r.pnl for r in res)
        neg += net < 0
        wins = sum(1 for r in res if r.pnl > 0)
        print(f"{a:%m-%d}..{b:%m-%d}   {len(res):5d} ${net:8,.0f} {wins/len(res)*100:5.1f} "
              f"  ${sum(r.pnl for r in exi):14,.0f}")
    print(f"  -> resolution bucket negative in {neg}/{n_win} windows")


def drawdown(rep):
    """Worst peak-to-trough on the cumulative curve, whole book and per bucket.

    Expectancy being positive does not make the tail affordable on a $500
    bankroll — that is the question the two live losses actually raised.
    """
    from pmbot.backtest import max_drawdown
    print(f"\n=== drawdown ===")
    for label, rs in (("whole book", rep.results),
                      ("resolution only",
                       [r for r in rep.results if r.closed_by == "resolution"])):
        by_t = sorted(rs, key=lambda r: r.entry_ts)   # see windows() on resolve_ts
        cum, run = [], 0.0
        for r in by_t:
            run += r.pnl
            cum.append(run)
        print(f"  {label:18s} maxDD ${max_drawdown(cum):8,.0f}   "
              f"worst single ${min((r.pnl for r in rs), default=0):,.0f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--db", default="pmbot.db")
    ap.add_argument("--leaders", nargs="*")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    leaders = args.leaders or followed_leaders(args.db)
    if not leaders:
        print("no leaders", file=sys.stderr)
        return 1
    s = get_settings()
    print(f"{len(leaders)} leaders | {args.days}d | floor ${s.copy_min_leader_notional_usd:,.0f} "
          f"| band {BAND} | cap ${s.max_per_market_usd:,.0f} | frac {s.copy_fraction}")

    data, gamma = PolymarketDataClient(), GammaClient()
    bt = ExactCopyBacktester(data, gamma, trades_limit=args.limit)
    if os.path.exists(CACHE) and not args.refresh:
        with open(CACHE, "rb") as f:
            tapes = pickle.load(f)
        print(f"tapes from cache ({sum(len(v) for v in tapes.values())} trades)")
    else:
        t0 = time.time()
        tapes = bt.fetch_tapes(leaders)
        with open(CACHE, "wb") as f:
            pickle.dump(tapes, f)
        print(f"fetched {sum(len(v) for v in tapes.values())} trades in {time.time()-t0:.0f}s")

    # Quote the book once for the whole tape, whichever branch supplied it:
    # `simulate` prices every fill off it and skips what it cannot quote, as
    # the live executor does. Without this the run fills at the leader's price.
    shared_book(bt, tapes)

    def run(**kw):
        return bt.simulate(
            tapes, lookback_days=args.days, price_min=BAND[0], price_max=BAND[1],
            min_leader_notional=s.copy_min_leader_notional_usd, **kw)

    base = run()
    show(base, f"BASELINE — live params, {args.days}d")
    by_leader(base)
    windows(base, args.days)
    drawdown(base)

    # Candidate mitigation: skip entries placed close to the market's close.
    # These are the in-game bets our lag copies worst AND the ones with no time
    # left to recover, so if the tail lives anywhere it should be here.
    for h in (1.0, 3.0, 6.0, 12.0):
        rep = run(min_hours_to_resolution=h)
        show(rep, f"min_hours_to_resolution={h}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
