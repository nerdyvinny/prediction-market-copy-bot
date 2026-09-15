"""How long would each candidate tie up the bankroll?

ROI is not the only way a leader can hurt this bot, and on 2026-09-03 it was
not the way that hurt. The bankroll rule is

    free = bankroll + min(0, realized_pnl) - deployed

so a leader whose positions resolve in WEEKS parks capital that cannot fund
anything else, and realized losses shrink the base underneath it. On
2026-09-02 those two met: $371 deployed against $500 with -$129.45 realized
left **-$0.45** free, and the bot refused every entry for eight days while
logging healthy cycles. It took a manual liquidation to unstick it.

Measured over the live tape, the horizon difference between wallets is not
subtle:

    current roster      n=15   median hold   4.1h   p90  11.7h   >7d:  0.0%
    everyone else       n=290  median hold   0.8h   p90 199.8h   >7d: 10.3%
    0x5cd5c8d7a1        n=39   median hold 215.2h           max 665.3h

So a candidate's hold distribution is a SAFETY property, separate from its
return, and a wallet that scores well on long-dated markets is exactly the one
that can deadlock the account. This reports it per candidate, alongside the
share of its capital that would sit in positions open longer than a week.

Usage:
    python -m scripts.candidate_horizon                       # the strict list
    python -m scripts.candidate_horizon --wallets 0xabc,0xdef
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.leaders import load_leader_config
from scripts._book import shared_book

BANKROLL, BAND = 500.0, (0.15, 0.85)
MIN_LEADER_NOTIONAL, SLIPPAGE_BPS, BOOK_STALENESS = 150.0, 60.0, 180.0
_TMP = os.environ.get("TEMP", "/tmp")
TAPE_CACHE = os.path.join(_TMP, "pmb_lb_tapes.pkl")
MKT_CACHE = os.path.join(_TMP, "pmb_lb_markets.pkl")


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--candidates", default=os.path.join(_TMP, "pmb_strict.json"))
    ap.add_argument("--wallets", default="", help="comma-separated, overrides --candidates")
    ap.add_argument("--long-hours", type=float, default=168.0,
                    help="a hold beyond this is 'long' (default one week)")
    args = ap.parse_args()

    roster = [w.lower() for w in load_leader_config().roster]
    if args.wallets:
        wallets = [w.strip().lower() for w in args.wallets.split(",") if w.strip()]
    else:
        try:
            wallets = [c["w"] for c in json.load(open(args.candidates))]
        except (OSError, ValueError):
            print(f"no candidate file at {args.candidates} — run scripts.census_strict first")
            return 1
    wallets = list(dict.fromkeys(wallets + roster))     # roster as the control

    tapes_all = pickle.load(open(TAPE_CACHE, "rb"))
    missing = [w for w in wallets if w not in tapes_all]
    if missing:
        print(f"no tape held for {len(missing)} wallet(s); skipping them")
    tapes = {w: tapes_all[w] for w in wallets if w in tapes_all}
    if not tapes:
        print("nothing to measure")
        return 1

    s = get_settings().model_copy(update={
        "bankroll_usd": BANKROLL, "copy_fraction": 0.10, "max_per_market_pct": 0.03,
        "max_per_leader_usd": 400.0, "compound_profits": False,
        "copy_price_min": BAND[0], "copy_price_max": BAND[1],
        "copy_min_leader_notional_usd": MIN_LEADER_NOTIONAL, "slippage_bps": SLIPPAGE_BPS,
    })
    data, gamma = PolymarketDataClient(bypass_cache=False), GammaClient()
    bt = ExactCopyBacktester(data, gamma, s, slippage_bps=SLIPPAGE_BPS)
    if os.path.exists(MKT_CACHE):
        with open(MKT_CACHE, "rb") as fh:
            bt._mkt_cache.update(pickle.load(fh))
    now = datetime.now(timezone.utc)
    shared_book(bt, tapes, quiet=True, lookback_days=args.days, now=now)

    print(f"\nhold time per candidate, {args.days}-day window "
          f"(entry -> resolution, as the bankroll sees it)")
    print("-" * 100)
    print(f"{'wallet':14s} {'n':>4s} {'median':>9s} {'p90':>9s} {'max':>9s} "
          f"{'$ in >1wk':>10s} {'share':>7s} {'no-time':>8s}")
    print("-" * 100)
    rows = []
    for w in wallets:
        if w not in tapes:
            continue
        rep = bt.simulate({w: tapes[w]}, lookback_days=args.days, now=now,
                          min_leader_notional=MIN_LEADER_NOTIONAL,
                          price_min=BAND[0], price_max=BAND[1], settings=s,
                          slippage_bps=SLIPPAGE_BPS, skip_round_tripped_entries=True,
                          book_max_staleness_seconds=BOOK_STALENESS)
        closed = [r for r in rep.results if max(r.resolve_ts, r.entry_ts) <= now]
        if not closed:
            continue
        # Gamma hands back a start-of-day end_date for a lot of markets, which
        # can precede the entry; the census clamps those to resolve_ts ==
        # entry_ts. That clamp is right for P&L and useless for a hold time --
        # it reads as an instant round trip and drags every median to 0.0h. So
        # measure the hold only where the two timestamps actually differ, and
        # say how much of the tape that left unmeasurable.
        pairs = [(r, (r.resolve_ts - r.entry_ts).total_seconds() / 3600.0)
                 for r in closed]
        timed = [(r, h) for r, h in pairs if h > 0.0]
        unmeasured = len(pairs) - len(timed)
        if not timed:
            continue
        hrs = [h for _, h in timed]
        long_usd = sum(r.size_usd for r, h in timed if h > args.long_hours)
        dep = sum(r.size_usd for r, _ in timed)
        rows.append((w, len(timed), pct(hrs, .5), pct(hrs, .9), max(hrs),
                     long_usd, long_usd / dep * 100 if dep else 0.0, unmeasured))
    rows.sort(key=lambda r: r[6])
    for w, n, med, p90, mx, lu, sh, un in rows:
        tag = " *LIVE" if w in roster else (" <-- parks capital" if sh > 20 else "")
        print(f"{w[:12]:14s} {n:4d} {med:8.1f}h {p90:8.1f}h {mx:8.1f}h "
              f"{lu:10,.0f} {sh:6.1f}% {un:8d}{tag}")
    print("-" * 100)
    print("'no-time' counts trades whose market carried no usable resolution stamp, so")
    print("their hold could not be measured at all -- read a row with a big one loosely.")
    print("'share' is the % of a candidate's deployed dollars that sat in positions")
    print(f"open longer than {args.long_hours:.0f}h. On a ${BANKROLL:,.0f} bankroll that "
          "capital funds nothing else,\nand the free-bankroll rule subtracts realized "
          "losses from what is left. See the module docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
