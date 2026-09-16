"""Score a wallet on non-overlapping windows that fit inside its OWN tape.

The 30/60/90-day bar cannot judge a wallet whose tape is shorter than 90 days.
Worse, it judges it WRONGLY and in the flattering direction: the windows all
return the same trades, but each is quoted over a different span, so the longer
window fills more of them. `census_strict` used to read that coverage gap as
out-of-sample evidence -- `0x4873713d49` scored a fictitious "+19.3% over days
60-90" on a 49-day tape holding nothing older than 49 days.

This does the test that tape CAN support: cut the wallet's own history into
equal non-overlapping windows and score each one separately, each with its own
quote book covering its own span, so no window is advantaged by coverage. Two
halves asks "did the older half predict the newer half"; three thirds is the
same question with less data per answer and is reported alongside.

It is still not a forward test. Every window here is in the past and the wallet
was chosen because its full-period number looked good, so a good split is the
weakest kind of pass -- it only rules out the case where one hot fortnight
carries everything. Judge the seat on the window after the day you add it.

Usage:
    python -m scripts.rescore_short_tape --wallet 0x4873713d49...
    python -m scripts.rescore_short_tape --wallet 0x... --splits 2,3,4
"""

from __future__ import annotations

import argparse
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
MKT_CACHE = os.path.join(_TMP, "pmb_lb_markets.pkl")


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--splits", default="2,3",
                    help="comma-separated: how many equal windows to cut the tape into")
    ap.add_argument("--trades-limit", type=int, default=2000)
    args = ap.parse_args()

    w = args.wallet.strip().lower()
    s = get_settings().model_copy(update={
        "bankroll_usd": BANKROLL, "copy_fraction": 0.10, "max_per_market_pct": 0.03,
        "max_per_leader_usd": 400.0, "compound_profits": False,
        "copy_price_min": BAND[0], "copy_price_max": BAND[1],
        "copy_min_leader_notional_usd": MIN_LEADER_NOTIONAL, "slippage_bps": SLIPPAGE_BPS,
    })
    data, gamma = PolymarketDataClient(bypass_cache=False), GammaClient()
    bt = ExactCopyBacktester(data, gamma, s, slippage_bps=SLIPPAGE_BPS,
                             trades_limit=args.trades_limit)
    if os.path.exists(MKT_CACHE):
        with open(MKT_CACHE, "rb") as fh:
            bt._mkt_cache.update(pickle.load(fh))

    print(f"fetching a fresh tape for {w} ...", flush=True)
    tape = bt.fetch_tapes([w]).get(w) or []
    if not tape:
        print("no tape returned")
        return 1
    stamps = [t.timestamp for t in tape]
    first, last = min(stamps), max(stamps)
    span = (last - first).total_seconds() / 86400.0
    now = datetime.now(timezone.utc)
    print(f"{len(tape)} trades, {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M}"
          f"  ({span:.1f} days, last trade {(now - last).total_seconds()/3600:.1f}h ago)")
    if len(tape) >= args.trades_limit:
        print(f"NOTE: the fetch returned its {args.trades_limit}-trade limit, so the tape is")
        print("      truncated -- the wallet is older than this window shows.")

    def score(win_end: datetime, win_days: float, label: str):
        shared_book(bt, {w: tape}, quiet=True, lookback_days=win_days, now=win_end)
        rep = bt.simulate({w: tape}, lookback_days=win_days, now=win_end,
                          min_leader_notional=MIN_LEADER_NOTIONAL,
                          price_min=BAND[0], price_max=BAND[1], settings=s,
                          slippage_bps=SLIPPAGE_BPS, skip_round_tripped_entries=True,
                          book_max_staleness_seconds=BOOK_STALENESS)
        closed = [r for r in rep.results if max(r.resolve_ts, r.entry_ts) <= now]
        raw = sum(1 for t in tape
                  if win_end - timedelta(days=win_days) <= t.timestamp < win_end)
        if not closed:
            print(f"  {label:26s} raw trades {raw:4d}   no closed copies")
            return None
        dep = sum(r.size_usd for r in closed)
        net = sum(r.pnl for r in closed)
        wins = sum(1 for r in closed if r.pnl > 0)
        hrs = [(r.resolve_ts - r.entry_ts).total_seconds() / 3600.0 for r in closed
               if r.resolve_ts > r.entry_ts]
        hold = f"med hold {pct(hrs, .5):6.1f}h" if hrs else "hold unmeasurable"
        print(f"  {label:26s} raw {raw:4d}  copies {len(closed):4d}  "
              f"dep ${dep:7,.0f}  net ${net:+7,.0f}  ROI {net/dep*100:+7.2f}%  "
              f"win {wins/len(closed)*100:4.1f}%  {hold}")
        return net / dep * 100.0

    print(f"\nwhole tape, scored as one window")
    score(last, span + 0.5, f"all {span:.0f} days")

    for k in [int(x) for x in args.splits.split(",") if x.strip()]:
        seg = span / k
        print(f"\n{k} non-overlapping windows of {seg:.1f} days each "
              f"(newest first; each quoted over its own span)")
        rois = []
        for i in range(k):
            end = last - timedelta(days=seg * i)
            lo = end - timedelta(days=seg)
            r = score(end, seg, f"{lo:%m-%d} .. {end:%m-%d}")
            rois.append(r)
        got = [r for r in rois if r is not None]
        if len(got) == k:
            verdict = "PASS - positive in every window" if all(r > 0 for r in got) \
                else "FAIL - negative in at least one window"
            print(f"  -> {verdict}")
        else:
            print(f"  -> INCONCLUSIVE - {k - len(got)} window(s) produced no closed copies")

    with open(MKT_CACHE, "wb") as fh:
        pickle.dump(bt._mkt_cache, fh)
    roster = [x.lower() for x in load_leader_config().roster]
    print(f"\n(live roster is {len(roster)} wallets; "
          f"{'this wallet IS on it' if w in roster else 'this wallet is NOT on it'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
