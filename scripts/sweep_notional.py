"""Walk-forward validation of the minimum-leader-notional floor.

The live bot copies a leader's BUY only if the leader staked at least
`PMBOT_COPY_MIN_LEADER_NOTIONAL_USD` (currently $300). On 2026-08-01 that floor
was rejecting ~93% of the followed cohort's tape, so this asks whether lowering
it actually earns more — or just trades more.

Structure copied from `walkforward_stop.py`, for the same reason: an in-sample
sweep picks the threshold with hindsight, which is exactly how you fool
yourself. Each fold here picks on a TRAIN slice and is scored on the TEST slice
that follows it, plus a fixed-threshold table where every level is scored
out-of-sample on the same slices.

Two arms, because they answer different questions:
  capped   — the bot's real $500 / $50-per-market limits: is it deployable?
             Lowering the floor multiplies trade count, and a $500 bankroll may
             simply not fit the extra trades — that is capital rationing, not
             signal.
  uncapped — limits removed so every threshold takes the same trades: is the
             per-dollar edge on small-notional copies actually real?

Usage:
    python -m scripts.sweep_notional --leaders 0xWALLET [...]
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
from scripts._book import shared_book


def followed_leaders(db_path: str) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("select wallet from followed_leaders")]
    finally:
        con.close()

THRESHOLDS = [0.0, 50.0, 100.0, 150.0, 200.0, 250.0, 300.0, 400.0, 500.0]
INCUMBENT = 300.0

# Production entry band (pmb-copy-params: do not move the 0.85 ceiling).
BAND = (0.15, 0.85)

TRAIN_DAYS = 30
TEST_DAYS = 15
STEP_DAYS = 15


def sim(bt, tapes, *, thr, now, days, settings):
    rep = bt.simulate(
        tapes,
        lookback_days=days,
        now=now,
        min_leader_notional=thr,
        price_min=BAND[0],
        price_max=BAND[1],
        settings=settings,
    )
    return rep.metrics()


def in_sample(bt, tapes, span, settings, label):
    print(f"\n=== IN-SAMPLE, full {span}d span — {label} ===")
    print("(hindsight view; the walk-forward tables below are the ones that count)")
    print(f"{'minNotional':>11} {'n':>5} {'invested':>11} {'net_pnl':>10} "
          f"{'roi%':>7} {'win%':>6} {'maxDD':>10}")
    for thr in THRESHOLDS:
        m = sim(bt, tapes, thr=thr, now=None, days=span, settings=settings)
        star = "  <== current" if thr == INCUMBENT else ""
        print(f"${thr:>10,.0f} {m['n_trades']:>5} ${m['invested']:>10,.0f} "
              f"${m['net_pnl']:>9,.0f} {m['roi']*100:>6.1f} {m['win_rate']*100:>5.1f} "
              f"${m['max_drawdown']:>9,.0f}{star}")


def wf_per_fold(bt, tapes, folds, settings, label):
    """Win rate / n / P&L for every threshold on every out-of-sample slice.

    The aggregate table hides whether a threshold wins consistently or rides
    one lucky fold — and hit rate is what moves independently of trade size,
    so it is the cleanest read on whether small copies are worse trades or
    merely smaller ones.
    """
    print(f"\n=== PER-FOLD OUT-OF-SAMPLE WIN RATE — {label} ===")
    for i, (_, test_end) in enumerate(folds, 1):
        print(f"  F{i}: test {(test_end - timedelta(days=TEST_DAYS)):%m-%d}..{test_end:%m-%d}")
    head = f"{'minNotional':>11}"
    for i in range(1, len(folds) + 1):
        head += f" {'F' + str(i) + ' win%(n)':>14}"
    head += f" {'overall win%':>13} {'spread':>7}"
    print(head)
    for thr in THRESHOLDS:
        cells, wins, tot = "", 0, 0
        rates = []
        for _, test_end in folds:
            m = sim(bt, tapes, thr=thr, now=test_end, days=TEST_DAYS, settings=settings)
            n = m["n_trades"]
            wr = m["win_rate"] * 100
            if n:
                rates.append(wr)
            wins += m["win_rate"] * n
            tot += n
            cells += f" {wr:>9.1f}({n:>3.0f})" if n else f" {'—':>14}"
        overall = (wins / tot * 100) if tot else 0.0
        spread = (max(rates) - min(rates)) if rates else 0.0
        star = "  <== current" if thr == INCUMBENT else ""
        print(f"${thr:>10,.0f}{cells} {overall:>12.1f} {spread:>6.1f}{star}")


def wf_fixed(bt, tapes, folds, settings, label):
    """Each threshold held fixed, scored only on out-of-sample test slices."""
    print(f"\n=== WALK-FORWARD, fixed threshold — {label} ===")
    print(f"{'minNotional':>11} {'oos n':>6} {'oos pnl':>10} {'roi%':>7} "
          f"{'worst DD':>10} {'folds>incumbent':>16}")
    per_fold: dict[float, list[float]] = {t: [] for t in THRESHOLDS}
    agg: dict[float, dict[str, float]] = {
        t: {"n": 0.0, "inv": 0.0, "pnl": 0.0, "worst": 0.0} for t in THRESHOLDS
    }
    for _, test_end in folds:
        for thr in THRESHOLDS:
            m = sim(bt, tapes, thr=thr, now=test_end, days=TEST_DAYS, settings=settings)
            per_fold[thr].append(m["net_pnl"])
            a = agg[thr]
            a["n"] += m["n_trades"]
            a["inv"] += m["invested"]
            a["pnl"] += m["net_pnl"]
            a["worst"] = max(a["worst"], m["max_drawdown"])

    totals = {}
    for thr in THRESHOLDS:
        agg_n, agg_inv = agg[thr]["n"], agg[thr]["inv"]
        agg_pnl, worst = agg[thr]["pnl"], agg[thr]["worst"]
        beats = sum(1 for a, b in zip(per_fold[thr], per_fold[INCUMBENT]) if a > b)
        totals[thr] = agg_pnl
        roi = (agg_pnl / agg_inv * 100) if agg_inv else 0.0
        star = "  <== current" if thr == INCUMBENT else ""
        print(f"${thr:>10,.0f} {agg_n:>6.0f} ${agg_pnl:>9,.0f} {roi:>6.1f} "
              f"${worst:>9,.0f} {beats:>10d}/{len(folds)}{star}")
    return totals, per_fold


def wf_adaptive(bt, tapes, folds, settings, label):
    """Pick on train by ROI, score on test — vs the incumbent held fixed."""
    print(f"\n=== WALK-FORWARD, retuned each fold — {label} ===")
    print(f"{'train window':>16} {'picked':>8} | {'test n':>7} {'picked pnl':>11} "
          f"{'$300 pnl':>10} {'diff':>9}")
    tot_pick = tot_inc = 0.0
    for train_end, test_end in folds:
        best = None
        for thr in THRESHOLDS:
            m = sim(bt, tapes, thr=thr, now=train_end, days=TRAIN_DAYS, settings=settings)
            if m["n_trades"] == 0:
                continue
            if best is None or m["roi"] > best[1]["roi"]:
                best = (thr, m)
        if best is None:
            print(f"{train_end:%m-%d} … no trades in train slice")
            continue
        thr, tm = best
        got = sim(bt, tapes, thr=thr, now=test_end, days=TEST_DAYS, settings=settings)
        inc = sim(bt, tapes, thr=INCUMBENT, now=test_end, days=TEST_DAYS, settings=settings)
        tot_pick += got["net_pnl"]
        tot_inc += inc["net_pnl"]
        print(f"{(train_end - timedelta(days=TRAIN_DAYS)):%m-%d}-{train_end:%m-%d} "
              f"(n={tm['n_trades']:>3}) ${thr:>7,.0f} | {got['n_trades']:>7} "
              f"${got['net_pnl']:>10,.0f} ${inc['net_pnl']:>9,.0f} "
              f"${got['net_pnl']-inc['net_pnl']:>8,.0f}", flush=True)
    print(f"{'TOTAL out-of-sample':>26} | {'':>7} ${tot_pick:>10,.0f} "
          f"${tot_inc:>9,.0f} ${tot_pick-tot_inc:>8,.0f}")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaders", nargs="*", default=None)
    ap.add_argument("--span", type=int, default=90)
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--cache", default=os.environ.get("PMBOT_NOTIONAL_CACHE", ""),
                    help="pickle of (tapes, market cache); written if absent, "
                         "reused if present. Skips the ~15min fetch+resolve warm-up.")
    args = ap.parse_args(argv)

    s = get_settings()
    leaders = [w.lower() for w in (args.leaders or followed_leaders(s.db_path))]

    # Pin PRODUCTION sizing explicitly. .env is gitignored and drifts between
    # machines — this laptop still carries max_per_market_usd=100 while the VM
    # was halved to 50 on 2026-07-31.
    prod = s.model_copy(update={
        "bankroll_usd": 500.0,
        "max_per_market_usd": 50.0,
        "max_per_leader_usd": 400.0,
        "copy_fraction": 0.10,
        "slippage_bps": 60.0,
    })
    uncapped = prod.model_copy(update={
        "bankroll_usd": 1e9, "max_per_market_usd": 1e9, "max_per_leader_usd": 1e9,
    })

    print(f"leaders: {len(leaders)}")
    print(f"production sizing: bankroll ${prod.bankroll_usd:,.0f}, "
          f"${prod.max_per_market_usd:,.0f}/market, ${prod.max_per_leader_usd:,.0f}/leader, "
          f"frac {prod.copy_fraction}, slip {prod.slippage_bps:.0f}bps")
    print(f"entry band held at {BAND[0]}-{BAND[1]}\n")

    data, gamma = PolymarketDataClient(), GammaClient()
    bt = ExactCopyBacktester(data, gamma, trades_limit=args.limit)

    t0 = time.time()
    now = datetime.now(timezone.utc)
    if args.cache and os.path.exists(args.cache):
        with open(args.cache, "rb") as fh:
            tapes, bt._mkt_cache = pickle.load(fh)
        print(f"loaded cached tapes + resolutions from {args.cache}", flush=True)
    else:
        print(f"fetching tapes (limit {args.limit})…", flush=True)
        tapes = bt.fetch_tapes(leaders)
        for w, tape in tapes.items():
            span_s = ""
            if tape:
                oldest = min(t.timestamp for t in tape)
                span_s = f", oldest {(now-oldest).days}d ago"
            print(f"  {w[:12]}… {len(tape)} trades{span_s}", flush=True)
        base = bt.simulate(tapes, lookback_days=args.span, warn_no_book=False)
        print(f"\nwarming resolution cache: {base.metrics()['n_trades']} trades "
              f"({time.time()-t0:.0f}s)", flush=True)
        if args.cache:
            tmp = args.cache + ".tmp"
            with open(tmp, "wb") as fh:
                pickle.dump((tapes, bt._mkt_cache), fh)
            os.replace(tmp, args.cache)
            print(f"cached tapes + resolutions -> {args.cache}", flush=True)

    # Quote the book once for the whole tape, whichever branch supplied it.
    # Every fold prices off it; a trade it cannot quote is a trade the live bot
    # could not have made either.
    shared_book(bt, tapes)

    folds = []
    test_end = now
    while True:
        train_end = test_end - timedelta(days=TEST_DAYS)
        if (now - train_end).days + TRAIN_DAYS > args.span:
            break
        folds.append((train_end, test_end))
        test_end -= timedelta(days=STEP_DAYS)
    folds.reverse()
    print(f"{len(folds)} folds ({TRAIN_DAYS}d train -> {TEST_DAYS}d test, "
          f"step {STEP_DAYS}d)")

    for arm_settings, label in ((prod, "capped (real $500 bankroll)"),
                                (uncapped, "uncapped (same trades each arm)")):
        in_sample(bt, tapes, args.span, arm_settings, label)
        wf_fixed(bt, tapes, folds, arm_settings, label)
        wf_per_fold(bt, tapes, folds, arm_settings, label)
        wf_adaptive(bt, tapes, folds, arm_settings, label)

    data.close()
    gamma.close()


if __name__ == "__main__":
    main(sys.argv[1:])
