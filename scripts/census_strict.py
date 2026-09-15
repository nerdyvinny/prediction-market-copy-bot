"""Apply the strict bar to the 30/60/90-day censuses, and feed `roster_arms`.

`census_compare` answers "does this population have an edge at all". This
answers the next question: WHICH wallets clear the bar we actually select on,
and it writes them in the shape `roster_arms` expects.

That shape used to come from a file (`pmb_top25.json`) hand-built during the
2026-09-02 session by code that was never committed, so the census ->
strict-bar -> lineup pipeline could not be re-run end to end. This is that
missing step, written down.

THE BAR (unchanged from 2026-09-02, see docs and leaders.yaml):
  positive over 30, 60 AND 90 days, and positive in BOTH non-overlapping
  slices, on at least `--min-n` closed copies in the 90-day window.

THE SLICES are derived by subtraction:
      recent = days  0-30   (the 30-day run)
      middle = days 30-60   (60-day run minus 30-day run)
      older  = days 60-90   (90-day run minus 60-day run)
This is an APPROXIMATION and `census_compare` says so for the same reason: a
longer run holds different bankroll state at every instant, so the 90-day sim's
trades inside days 0-30 are not identical to the 30-day sim's. Slices are
directionally right; they are not a substitute for three non-overlapping runs.
A wallet that only just clears on a slice has not really been tested.

**The subtraction lies outright when the wallet has no tape that far back**, and
it lies in the flattering direction. `0x4873713d49` has 692 trades spanning 49
days -- nothing at all between 60 and 90 days ago -- yet its 90-day run scored
185 closed trades against the 60-day run's 123. The extra 62 are not older
trades. They are the SAME trades, filled in the 90-day run and skipped in the
60-day one, because `shared_book` discovers quotes over the window it is given
and a trade with no quote is a trade that does not happen. Subtracting the two
runs turns that coverage difference into a fictitious "+19.3% over days 60-90",
and the wallet clears the one out-of-sample test the bar exists to apply.

So each slice is now checked against the RAW TAPE: unless the wallet actually
traded in that window (`--min-slice-trades`), the slice is None and the wallet
fails for lack of evidence rather than passing on an artifact. Two of the twelve
that cleared the 2026-09-14 run were this, and one of them was nearly deployed.

THE SCORE is a shrunk ROI, not a raw one. Raw ROI ranks thin samples to the
top -- [[pmb-wallet-census]] measured the positive tail as thin samples rather
than skill, and corr(trade count, ROI) = -0.015. So each wallet's 90-day ROI is
pulled toward the population mean in proportion to how little tape backs it:

      shrunk = (n * roi + k * pop_mean) / (n + k)

with k = `--shrink` (default 50, i.e. a wallet needs ~50 closed copies before
its own number outweighs the population's). This only ORDERS candidates that
have already cleared the bar; it is not a forecast, and nothing in this repo
has ever shown a wallet score that predicts the next window.

Usage:
    python -m scripts.census_strict                   # table + write the file
    python -m scripts.census_strict --min-n 100       # the believable-sample bar
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
from datetime import datetime, timedelta, timezone

_TMP = os.environ.get("TEMP", "/tmp")
TAPE_CACHE = os.path.join(_TMP, "pmb_lb_tapes.pkl")
WINDOWS = (30, 60, 90)


def raw_trades_in(tape, lo_days: int, hi_days: int, now: datetime) -> int:
    """Trades the wallet really made between `hi_days` and `lo_days` ago.

    Counted off the fetched tape, before any copy filter — this asks whether
    there is anything to measure in the window, not whether we could copy it.
    """
    lo, hi = now - timedelta(days=lo_days), now - timedelta(days=hi_days)
    return sum(1 for t in tape if hi <= t.timestamp < lo)


def load(days: int) -> dict[str, dict]:
    p = os.path.join(_TMP, f"pmb_census_{days}d.json")
    try:
        with open(p) as fh:
            return {r["wallet"]: r for r in json.load(fh)}
    except (OSError, ValueError):
        return {}


def slice_roi(wide: dict | None, narrow: dict | None) -> tuple[float | None, float]:
    """ROI over the span the WIDE window covers but the NARROW one does not."""
    if not wide:
        return None, 0.0
    dep = wide["deployed"] - (narrow["deployed"] if narrow else 0.0)
    net = wide["net"] - (narrow["net"] if narrow else 0.0)
    n = wide["n"] - (narrow["n"] if narrow else 0)
    if dep <= 1e-9 or n <= 0:
        return None, 0.0          # no distinct trades in this slice: untested
    return net / dep * 100.0, dep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=50,
                    help="closed copies required in the 90-day window")
    ap.add_argument("--shrink", type=float, default=50.0,
                    help="prior weight k in the shrunk score")
    ap.add_argument("--out", default=os.path.join(_TMP, "pmb_strict.json"))
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-slice-trades", type=int, default=10,
                    help="raw trades the wallet must have made inside a slice "
                         "for that slice to count as evidence at all")
    args = ap.parse_args()

    try:
        tapes = pickle.load(open(TAPE_CACHE, "rb"))
    except (OSError, ValueError):
        print(f"no tape cache at {TAPE_CACHE} — the slice check needs it")
        return 1
    now = datetime.now(timezone.utc)

    data = {d: load(d) for d in WINDOWS}
    missing = [d for d in WINDOWS if not data[d]]
    if missing:
        print(f"missing census windows: {missing} — run scripts.wallet_census "
              f"for each of {WINDOWS} first")
        return 1

    scored = [r for r in data[90].values() if r["n"] > 0]
    pop_mean = sum(r["roi"] for r in scored) / len(scored) if scored else 0.0
    print(f"90-day population: {len(scored)} wallets scored, mean ROI "
          f"{pop_mean:+.2f}%  (the prior the shrunk score pulls toward)")

    rows = []
    for w, r90 in data[90].items():
        r30, r60 = data[30].get(w), data[60].get(w)
        mid, mid_dep = slice_roi(r60, r30)       # days 30-60
        old, old_dep = slice_roi(r90, r60)       # days 60-90
        # A slice the wallet did not trade in is not evidence, however good the
        # subtraction looks. See the module docstring.
        tape = tapes.get(w) or []
        mid_raw = raw_trades_in(tape, 30, 60, now)
        old_raw = raw_trades_in(tape, 60, 90, now)
        if mid_raw < args.min_slice_trades:
            mid, mid_dep = None, 0.0
        if old_raw < args.min_slice_trades:
            old, old_dep = None, 0.0
        n = r90["n"]
        rows.append({
            "w": w,
            "name": r90.get("name", ""),
            "r30": r30["roi"] if r30 else None,
            "r60": r60["roi"] if r60 else None,
            "r90": r90["roi"],
            "n30": r30["n"] if r30 else 0,
            "n60": r60["n"] if r60 else 0,
            "n90": n,
            "mid": mid, "old": old,
            "mid_dep": round(mid_dep, 2), "old_dep": round(old_dep, 2),
            "mid_raw": mid_raw, "old_raw": old_raw,
            "span": r90.get("span_days", 0),
            "roster": bool(r90.get("on_roster")),
            "shrunk": (n * r90["roi"] + args.shrink * pop_mean) / (n + args.shrink),
        })

    def clears(c: dict) -> bool:
        return (c["r30"] is not None and c["r60"] is not None
                and c["r30"] > 0 and c["r60"] > 0 and c["r90"] > 0
                and c["mid"] is not None and c["old"] is not None
                and c["mid"] > 0 and c["old"] > 0
                and c["n90"] >= args.min_n)

    strict = sorted((c for c in rows if clears(c)), key=lambda c: -c["shrunk"])

    # Report the funnel, not just the survivors — how many wallets each clause
    # removes is the finding whenever the survivor list is short.
    def count(pred) -> int:
        return sum(1 for c in rows if pred(c))
    print(f"\nfunnel over {len(rows)} wallets with a 90-day score:")
    print(f"  {count(lambda c: c['n90'] >= args.min_n):5d} have >= {args.min_n} closed copies in 90d")
    print(f"  {count(lambda c: c['n90'] >= args.min_n and c['r90'] > 0):5d}  ... and are positive over 90d")
    print(f"  {count(lambda c: c['n90'] >= args.min_n and c['r90'] > 0 and (c['r30'] or 0) > 0 and (c['r60'] or 0) > 0):5d}  ... and over 30d and 60d too")
    print(f"  {len(strict):5d}  ... and positive in BOTH non-overlapping slices  <- the bar")

    print(f"\n{'-' * 118}")
    print(f"{'wallet':14s} {'name':16s} {'r30':>8s} {'r60':>8s} {'r90':>8s} "
          f"{'mid':>8s} {'old':>8s} {'n90':>5s} {'shrunk':>8s}")
    print("-" * 118)
    for c in strict[:args.top]:
        tag = " *" if c["roster"] else ""
        print(f"{c['w'][:12]:14s} {str(c['name'])[:16]:16s} "
              f"{c['r30']:+8.1f} {c['r60']:+8.1f} {c['r90']:+8.1f} "
              f"{c['mid']:+8.1f} {c['old']:+8.1f} {c['n90']:5d} "
              f"{c['shrunk']:+8.2f}{tag}")
    if not strict:
        print("  (nothing clears the bar)")
    print("-" * 118)
    print("* = already on the live roster.  mid = days 30-60, old = days 60-90, "
          "both by subtraction.")

    with open(args.out, "w") as fh:
        json.dump(strict, fh, indent=1)
    print(f"\nwrote {len(strict)} candidates to {args.out}")
    print("next: python -m scripts.roster_arms --days 90 --candidates "
          f"{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
