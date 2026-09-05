"""Walk-forward validation of the max-time-to-resolution ceiling.

`sweep_horizon.py` picks the best ceiling by looking at the whole window —
which is exactly how you fool yourself. Here each fold chooses a ceiling on a
TRAIN slice and is scored on the TEST slice that follows it, which the choice
never saw.

Folds roll forward:  [train 30d][test 15d] -> step 15d -> repeat.

Reported per fold: the ceiling train picked, what it earned on test, and what
no ceiling earned on the same test slice. If the ceiling only wins because it
was chosen with hindsight, the test columns give it away.

Two arms, because they answer different questions:
  capped   — the bot's real $500 / $50-per-market limits: is it deployable?
  uncapped — limits removed so the ceiling is the only variable: is it real?

Usage:
    python -m scripts.walkforward_horizon [--leaders 0xWALLET ...]
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from scripts._book import shared_book
from scripts.sweep_horizon import HORIZONS, followed_leaders, load_tapes

TRAIN_DAYS = 30
TEST_DAYS = 15
STEP_DAYS = 15


def tag(hours: float | None) -> str:
    if hours is None:
        return "off"
    return f"{hours/24:.0f}d" if hours >= 24 else f"{hours:.0f}h"


def run(bt, tapes, *, now, days, settings, floor, hours) -> dict:
    rep = bt.simulate(
        tapes, lookback_days=days, now=now, settings=settings,
        min_leader_notional=floor, max_hours_to_resolution=hours or 0.0,
    )
    return rep.metrics()


def pick(bt, tapes, *, now, days, settings, floor) -> tuple:
    """Best ceiling on this slice, chosen by ROI (trade counts differ)."""
    best = None
    for hours in HORIZONS:
        m = run(bt, tapes, now=now, days=days, settings=settings,
                floor=floor, hours=hours)
        if m["n_trades"] == 0:
            continue
        if best is None or m["roi"] > best[1]["roi"]:
            best = (hours, m)
    return best if best else (None, None)


def run_arm(bt, tapes, folds, settings, floor, label: str) -> None:
    print(f"\n=== {label} ===")
    print(f"  {'train window':>26} {'picked':>7} | {'test n':>7} {'test pnl':>10} "
          f"{'no-cap pnl':>11} {'diff':>9} {'test DD':>9} {'noCap DD':>10}")
    wins = losses = ties = 0
    total_diff = 0.0
    for train_end in folds:
        chosen, _tm = pick(bt, tapes, now=train_end, days=TRAIN_DAYS,
                           settings=settings, floor=floor)
        test_end = train_end + timedelta(days=TEST_DAYS)
        t_on = run(bt, tapes, now=test_end, days=TEST_DAYS,
                   settings=settings, floor=floor, hours=chosen)
        t_off = run(bt, tapes, now=test_end, days=TEST_DAYS,
                    settings=settings, floor=floor, hours=None)
        diff = t_on["net_pnl"] - t_off["net_pnl"]
        total_diff += diff
        if abs(diff) < 0.005:
            ties += 1
        elif diff > 0:
            wins += 1
        else:
            losses += 1
        # ASCII only: this prints to a cp1252 console on Windows.
        window = (f"{(train_end - timedelta(days=TRAIN_DAYS)):%m-%d}"
                  f"..{train_end:%m-%d}")
        print(f"  {window:>26} {tag(chosen):>7} | {t_on['n_trades']:>7} "
              f"${t_on['net_pnl']:>9,.2f} ${t_off['net_pnl']:>10,.2f} "
              f"${diff:>8,.2f} ${t_on['max_drawdown']:>8,.2f} "
              f"${t_off['max_drawdown']:>9,.2f}", flush=True)
    print(f"  -> folds where the ceiling beat doing nothing: {wins}W / {losses}L"
          f"{f' / {ties} tie' if ties else ''};  total diff ${total_diff:,.2f}")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaders", nargs="*", default=None)
    ap.add_argument("--span", type=int, default=120,
                    help="total history to roll folds through")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--floor", type=float, default=None,
                    help="min leader notional; defaults to settings")
    args = ap.parse_args(argv)

    s = get_settings()
    if args.floor is not None:
        s = s.model_copy(update={"copy_min_leader_notional_usd": args.floor})
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
    print(f"({time.time()-t0:.0f}s)", flush=True)

    # Fold ends: the last one must leave a full TEST_DAYS of tape after it.
    start = now - timedelta(days=args.span)
    folds = []
    train_end = start + timedelta(days=TRAIN_DAYS)
    while train_end + timedelta(days=TEST_DAYS) <= now:
        folds.append(train_end)
        train_end += timedelta(days=STEP_DAYS)
    print(f"{len(folds)} folds: train {TRAIN_DAYS}d / test {TEST_DAYS}d / "
          f"step {STEP_DAYS}d over the last {args.span}d")

    floor = s.copy_min_leader_notional_usd
    run_arm(bt, tapes, folds, None, floor,
            "capped (the bot's real $500 / $50-per-market limits)")
    uncapped = s.model_copy(update={
        "bankroll_usd": 1e9, "max_per_market_usd": 1e9, "max_per_leader_usd": 1e9,
    })
    run_arm(bt, tapes, folds, uncapped, floor,
            "uncapped (ceiling is the only variable)")

    data.close()
    gamma.close()


if __name__ == "__main__":
    main(sys.argv[1:])
