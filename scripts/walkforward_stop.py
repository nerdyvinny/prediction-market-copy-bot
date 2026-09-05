"""Walk-forward validation of the stop-loss level.

`sweep_stop.py` picks the best stop by looking at the whole window — which is
exactly how you fool yourself. Here each fold chooses a level on a TRAIN slice
and is scored on the TEST slice that follows it, which the choice never saw.

Folds roll forward:  [train 30d][test 15d] -> step 15d -> repeat.

Reported per fold: the level train picked, what it earned on test, and what
doing nothing earned on the same test slice. If the stop only wins because it
was chosen with hindsight, the test columns give it away.

Two arms, because they answer different questions:
  capped   — the bot's real $500 / $50-per-market limits: is it deployable?
  uncapped — limits removed so every arm takes the same trades: is it real?

Usage:
    python -m scripts.walkforward_stop --leaders 0xWALLET [...]
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.data.price_cache import PriceCache
from scripts._book import shared_book
from scripts.sweep_stop import (
    STOPS, derive_resolutions, fetch_series, followed_leaders, load_cache,
)

TRAIN_DAYS = 30
TEST_DAYS = 15
STEP_DAYS = 15


def pick(bt, tapes, *, now, days, resolves, series, settings) -> tuple:
    """Best stop level on this slice, chosen by ROI (trade counts differ)."""
    best = None
    for stop in STOPS:
        rep = bt.simulate(
            tapes, lookback_days=days, now=now, resolve_at=resolves, settings=settings,
            stop_loss_frac=stop, price_series=series if stop else None,
        )
        m = rep.metrics()
        if m["n_trades"] == 0:
            continue
        if best is None or m["roi"] > best[1]["roi"]:
            best = (stop, m)
    return best if best else (None, None)


def score(bt, tapes, *, now, days, resolves, series, settings, stop) -> dict:
    rep = bt.simulate(
        tapes, lookback_days=days, now=now, resolve_at=resolves, settings=settings,
        stop_loss_frac=stop, price_series=series if stop else None,
    )
    m = rep.metrics()
    m["stopped"] = sum(1 for r in rep.results if r.closed_by == "stop-loss")
    return m


def run_arm(bt, tapes, folds, resolves, series, settings, label: str) -> None:
    print(f"\n=== {label} ===")
    print(f"{'train window':>26} {'picked':>7} | {'test n':>7} {'test pnl':>10} "
          f"{'no-stop pnl':>12} {'diff':>9} {'test DD':>9} {'noStop DD':>10}")
    tot_stop = tot_none = 0.0
    dd_stop = dd_none = 0.0
    for train_end, test_end in folds:
        stop, tm = pick(bt, tapes, now=train_end, days=TRAIN_DAYS,
                        resolves=resolves, series=series, settings=settings)
        if tm is None:
            print(f"{train_end:%m-%d} … no trades in train slice")
            continue
        got = score(bt, tapes, now=test_end, days=TEST_DAYS, resolves=resolves,
                    series=series, settings=settings, stop=stop)
        flat = score(bt, tapes, now=test_end, days=TEST_DAYS, resolves=resolves,
                     series=series, settings=settings, stop=None)
        tot_stop += got["net_pnl"]
        tot_none += flat["net_pnl"]
        dd_stop = max(dd_stop, got["max_drawdown"])
        dd_none = max(dd_none, flat["max_drawdown"])
        lbl = "none" if stop is None else f"-{stop*100:.0f}%"
        win = got["net_pnl"] - flat["net_pnl"]
        print(f"{(train_end - timedelta(days=TRAIN_DAYS)):%m-%d}-{train_end:%m-%d} "
              f"(n={tm['n_trades']:>3}) {lbl:>7} | {got['n_trades']:>7} "
              f"${got['net_pnl']:>9,.0f} ${flat['net_pnl']:>11,.0f} "
              f"${win:>8,.0f} ${got['max_drawdown']:>8,.0f} "
              f"${flat['max_drawdown']:>9,.0f}", flush=True)

    print(f"{'TOTAL out-of-sample':>34} | {'':>7} ${tot_stop:>9,.0f} "
          f"${tot_none:>11,.0f} ${tot_stop-tot_none:>8,.0f} "
          f"${dd_stop:>8,.0f} ${dd_none:>9,.0f}")
    if tot_none:
        print(f"{'':>34}   walk-forward stop vs no stop: "
              f"{100*(tot_stop-tot_none)/abs(tot_none):+.1f}% on P&L, "
              f"{100*(dd_stop-dd_none)/abs(dd_none):+.1f}% on worst drawdown")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaders", nargs="*", default=None)
    ap.add_argument("--span", type=int, default=90, help="total history to cover")
    ap.add_argument("--limit", type=int, default=3000)
    args = ap.parse_args(argv)

    s = get_settings()
    leaders = [w.lower() for w in (args.leaders or followed_leaders(s.db_path))]
    data, gamma, prices = PolymarketDataClient(), GammaClient(), PriceCache()
    bt = ExactCopyBacktester(data, gamma, trades_limit=args.limit)

    t0 = time.time()
    print(f"fetching tapes for {len(leaders)} leaders (limit {args.limit})…", flush=True)
    tapes = bt.fetch_tapes(leaders)
    # Quote the book once for the whole tape: `simulate` prices every fill
    # off it and skips what it cannot quote, exactly as the live executor
    # does. Without this the run fills at the leader's own price.
    shared_book(bt, tapes)
    for w, tape in tapes.items():
        span = ""
        if tape:
            oldest = min(t.timestamp for t in tape)
            span = f", oldest {(datetime.now(timezone.utc)-oldest).days}d ago"
        print(f"  {w[:12]}… {len(tape)} trades{span}", flush=True)

    full = bt.simulate(tapes, lookback_days=args.span)
    print(f"\nfull-span baseline: {full.metrics()['n_trades']} trades "
          f"({time.time()-t0:.0f}s)", flush=True)

    series = fetch_series(full.results, prices, load_cache())
    resolves = derive_resolutions(series, full.results)
    print(f"price history for {sum(1 for v in series.values() if v)}/{len(series)} "
          f"tokens; {len(resolves)} resolution times derived\n", flush=True)

    now = datetime.now(timezone.utc)
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

    uncapped = s.model_copy(update={
        "bankroll_usd": 1e9, "max_per_market_usd": 1e9, "max_per_leader_usd": 1e9,
    })
    run_arm(bt, tapes, folds, resolves, series, s, "capped (real $500 bankroll)")
    run_arm(bt, tapes, folds, resolves, series, uncapped, "uncapped (same trades each arm)")

    data.close()
    gamma.close()
    prices.close()


if __name__ == "__main__":
    main(sys.argv[1:])
