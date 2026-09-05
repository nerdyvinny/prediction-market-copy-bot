"""Walk-forward validation of punching a hole in the middle of the copy band.

Where this came from: the LIVE tape (49 closed trades, 2026-07-27..08-13) shows
entries priced 0.60-0.80 at -34% ROI on $650 invested, while cheaper and dearer
entries both made money. That is four bad trades out of fourteen, so it is a
hunch, not a finding. This script is the gate it has to pass.

Two things make this test harder than it looks:

1. The band was CHOSEN by staring at live results. Scoring it on a window that
   overlaps live trading is in-sample by construction — the same trap as vetting
   the current leader cohort on the window it was selected from. So every fold
   is labelled clean/contaminated by whether its TEST slice ends before live
   trading began (LIVE_START), and the two are tallied separately. Only the
   clean folds are evidence.
2. A band filter changes trade COUNT, not just P&L, and a filter that skips
   everything trivially "wins" a losing window. Each fold therefore prints how
   many entries the band actually removed; a fold that removed none is a tie
   carrying no information and is reported as such.

Folds roll forward:  [train 30d][test 15d] -> step 15d -> repeat.

Three arms, because they answer different questions:
  capped    — the bot's real limits (3% of bankroll per market): deployable?
  uncapped  — limits removed so the band is the only variable: is it real?
  fixed     — 0.60-0.80 punched out on every fold, never chosen by train. This
              is the actual question the live tape raised; the trained arms ask
              the weaker "is SOME band learnable".

Usage:
    python -m scripts.walkforward_price_band [--leaders 0xWALLET ...]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.models import Side
from scripts._book import shared_book
from scripts.sweep_horizon import followed_leaders

TRAIN_DAYS = 30
TEST_DAYS = 15
STEP_DAYS = 15

# First live fill, from the production ledger. A test slice ending at or before
# this date cannot have informed the 0.60-0.80 hunch.
LIVE_START = datetime(2026, 7, 27, tzinfo=timezone.utc)

# The hunch, plus neighbours wide and narrow enough to show whether any signal
# is a band or just the two worst trades wearing one.
BANDS: list[tuple[float, float] | None] = [
    None,               # filter off - today's behaviour
    (0.60, 0.80),       # the live tape's money pit
    (0.60, 0.70),
    (0.70, 0.80),
    (0.65, 0.75),
    (0.55, 0.85),
]
HUNCH = (0.60, 0.80)

CACHE = os.environ.get("PMBOT_TAPE_CACHE", ".tapes_band.pkl")

# Price buckets for the census. Upper bound exclusive, mirroring the filter.
BUCKETS = [(0.15, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.86)]


def tag(band: tuple[float, float] | None) -> str:
    return "off" if band is None else f"{band[0]:.2f}-{band[1]:.2f}"


def load_tapes(bt, leaders: list[str], refresh: bool) -> dict:
    """Tapes are multi-MB and slow to page; cache so re-runs are free."""
    import pickle

    if not refresh:
        try:
            with open(CACHE, "rb") as fh:
                cached = pickle.load(fh)
            if set(cached) >= {w.lower() for w in leaders}:
                print(f"tapes: loaded {len(cached)} from {CACHE}", flush=True)
                return {w.lower(): cached[w.lower()] for w in leaders}
        except (OSError, ValueError, pickle.UnpicklingError):
            pass
    print(f"fetching tapes for {len(leaders)} leaders...", flush=True)
    tapes = bt.fetch_tapes(leaders)
    with open(CACHE + ".tmp", "wb") as fh:
        pickle.dump(tapes, fh)
    os.replace(CACHE + ".tmp", CACHE)
    return tapes


def census(bt, tapes: dict, *, now: datetime, span: int, s) -> None:
    """How many scorable entries sit in each price bucket.

    The horizon filter taught this lesson the expensive way: a sweep row over a
    bucket the backtest can barely see is noise wearing a number. Counts BUYs
    that clear the live entry gates, split into resolved (scorable) and still
    open (silently dropped by the backtest).
    """
    cutoff = now - timedelta(days=span)
    rows = {b: {"resolved": 0, "open": 0} for b in BUCKETS}
    unknown = 0
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
            for b in BUCKETS:
                if b[0] <= t.price < b[1]:
                    scorable = market.closed and winner is not None
                    rows[b]["resolved" if scorable else "open"] += 1
                    break
    print(f"\n=== census: copyable BUYs by entry price, last {span}d ===")
    print(f"  {'band':>12} {'resolved':>9} {'open':>7} {'% scorable':>11}")
    for b in BUCKETS:
        r, o = rows[b]["resolved"], rows[b]["open"]
        pct = (100.0 * r / (r + o)) if (r + o) else 0.0
        print(f"  {tag(b):>12} {r:>9} {o:>7} {pct:>10.0f}%")
    if unknown:
        print(f"  ({unknown} entries on markets Gamma could not resolve)")


def run(bt, tapes, *, now, days, settings, floor, band) -> dict:
    rep = bt.simulate(
        tapes, lookback_days=days, now=now, settings=settings,
        min_leader_notional=floor, skip_price_band=band,
        skip_round_tripped_entries=True,
    )
    return rep.metrics()


def pick(bt, tapes, *, now, days, settings, floor):
    """Best band on this slice by ROI (trade counts differ between bands)."""
    best = None
    for band in BANDS:
        m = run(bt, tapes, now=now, days=days, settings=settings,
                floor=floor, band=band)
        if m["n_trades"] == 0:
            continue
        if best is None or m["roi"] > best[1]["roi"]:
            best = (band, m)
    return best if best else (None, None)


def run_arm(bt, tapes, folds, settings, floor, label, *, fixed=None) -> None:
    print(f"\n=== {label} ===")
    print(f"  {'train window':>26} {'band':>11} {'clean':>6} | {'test n':>7} "
          f"{'skipped':>8} {'test pnl':>10} {'off pnl':>10} {'diff':>9} "
          f"{'test DD':>9}")
    tally = {True: [0, 0, 0, 0.0], False: [0, 0, 0, 0.0]}   # clean -> W,L,tie,diff
    for train_end in folds:
        test_end = train_end + timedelta(days=TEST_DAYS)
        band = fixed if fixed is not None else pick(
            bt, tapes, now=train_end, days=TRAIN_DAYS,
            settings=settings, floor=floor)[0]
        t_on = run(bt, tapes, now=test_end, days=TEST_DAYS,
                   settings=settings, floor=floor, band=band)
        t_off = run(bt, tapes, now=test_end, days=TEST_DAYS,
                    settings=settings, floor=floor, band=None)
        diff = t_on["net_pnl"] - t_off["net_pnl"]
        skipped = t_off["n_trades"] - t_on["n_trades"]
        clean = test_end <= LIVE_START
        slot = tally[clean]
        if abs(diff) < 0.005:
            slot[2] += 1
        elif diff > 0:
            slot[0] += 1
        else:
            slot[1] += 1
        slot[3] += diff
        # ASCII only: this prints to a cp1252 console on Windows.
        window = (f"{(train_end - timedelta(days=TRAIN_DAYS)):%m-%d}"
                  f"..{train_end:%m-%d}")
        print(f"  {window:>26} {tag(band):>11} {'yes' if clean else 'NO':>6} | "
              f"{t_on['n_trades']:>7} {skipped:>8} ${t_on['net_pnl']:>9,.2f} "
              f"${t_off['net_pnl']:>9,.2f} ${diff:>8,.2f} "
              f"${t_on['max_drawdown']:>8,.2f}", flush=True)
    for clean in (True, False):
        w, l, ti, d = tally[clean]
        if w + l + ti == 0:
            continue
        which = "CLEAN folds (test ends before live trading)" if clean else \
                "contaminated folds (overlap the window the band was read from)"
        print(f"  -> {which}: {w}W / {l}L"
              f"{f' / {ti} tie' if ti else ''};  total diff ${d:,.2f}")


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

    print(f"per-market cap in force: ${s.per_market_cap_usd:,.2f} "
          f"(bankroll ${s.bankroll_usd:,.0f}); notional floor "
          f"${s.copy_min_leader_notional_usd:,.0f}")

    census(bt, tapes, now=now, span=args.span, s=s)

    # Fold ends: the last one must leave a full TEST_DAYS of tape after it.
    start = now - timedelta(days=args.span)
    folds = []
    train_end = start + timedelta(days=TRAIN_DAYS)
    while train_end + timedelta(days=TEST_DAYS) <= now:
        folds.append(train_end)
        train_end += timedelta(days=STEP_DAYS)
    print(f"\n{len(folds)} folds: train {TRAIN_DAYS}d / test {TEST_DAYS}d / "
          f"step {STEP_DAYS}d over the last {args.span}d")

    floor = s.copy_min_leader_notional_usd
    uncapped = s.model_copy(update={
        "bankroll_usd": 1e9, "max_per_market_usd": 1e9,
        "max_per_market_pct": None, "max_per_leader_usd": 1e9,
    })
    run_arm(bt, tapes, folds, None, floor,
            f"FIXED {tag(HUNCH)} punched out, capped (the bot's real limits)",
            fixed=HUNCH)
    run_arm(bt, tapes, folds, uncapped, floor,
            f"FIXED {tag(HUNCH)} punched out, uncapped (band is the only variable)",
            fixed=HUNCH)
    run_arm(bt, tapes, folds, None, floor,
            "TRAINED band, capped (can a band be learned at all?)")

    data.close()
    gamma.close()


if __name__ == "__main__":
    main(sys.argv[1:])
