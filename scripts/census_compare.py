"""Compare the 30/60/90-day wallet censuses, and rank what survives all three.

`wallet_census.py` scores every wallet over one window. Run it three times and
you can ask two different questions, one of which is much weaker than it looks:

  1. IS THE POPULATION FINDING STABLE? Mean and median copy return, and the
     share of wallets that are profitable, at each window length. If the
     30-day answer (mean +0.57%, half profitable) was a one-month artifact,
     the 60- and 90-day numbers will say so.

  2. DOES A WALLET'S PAST PREDICT ITS FUTURE? This one needs care. The three
     windows are NESTED -- a wallet's 90-day result CONTAINS its 30-day
     result -- so correlating them measures mostly the overlap and would look
     impressive while proving nothing. The non-overlapping SLICES are the
     honest comparison:

         recent = days 0-30       (the 30-day run)
         middle = days 30-60      (60-day minus 30-day)
         older  = days 60-90      (90-day minus 60-day)

     Then: does `older` predict `recent`? That is a real forward test, and it
     is the same question the roster funnel failed.

     The subtraction is an APPROXIMATION and the report says so. A longer run
     has different bankroll state at every instant -- capital tied up in a
     day-80 position is unavailable on day 20 -- so the 90-day sim's trades
     inside days 0-30 are not identical to the 30-day sim's. Slices are
     directionally right and are not a substitute for running three
     non-overlapping windows directly. Treat a strong result here as a reason
     to run those, not as the finding itself.

Usage:
    python -m scripts.census_compare
    python -m scripts.census_compare --min-trades 20 --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st

_TMP = os.environ.get("TEMP", "/tmp")
WINDOWS = (30, 60, 90)


def load(days: int) -> dict[str, dict]:
    p = os.path.join(_TMP, f"pmb_census_{days}d.json")
    if not os.path.exists(p):
        return {}
    with open(p) as fh:
        return {r["wallet"]: r for r in json.load(fh)}


def spearman(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return 0.0

    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def summarize(rows: list[dict], label: str, min_trades: int) -> None:
    g = [r for r in rows if r["n"] >= min_trades]
    if not g:
        print(f"  {label:<26} (no wallets)")
        return
    roi = [r["roi"] for r in g]
    prof = sum(1 for r in g if r["net"] > 0)
    dep = sum(r["deployed"] for r in g)
    net = sum(r["net"] for r in g)
    sd = st.pstdev(roi)
    t = st.mean(roi) / (sd / math.sqrt(len(roi))) if sd else 0.0
    print(f"  {label:<26} n={len(g):>4}  profitable {prof / len(g) * 100:>3.0f}%  "
          f"mean {st.mean(roi):>+7.2f}%  median {st.median(roi):>+7.2f}%  "
          f"sd {sd:>5.1f}pp  t={t:>+5.2f}  pooled {net / dep * 100 if dep else 0:>+6.2f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    data = {d: load(d) for d in WINDOWS}
    have = [d for d in WINDOWS if data[d]]
    if not have:
        print("No census results found. Run scripts.wallet_census first.")
        return 1
    print("=" * 118)
    print("CENSUS COMPARISON -- the same wallets scored over 30, 60 and 90 days")
    print("=" * 118)
    for d in WINDOWS:
        print(f"  {d:>2}-day run: "
              + (f"{len(data[d])} wallets scored" if data[d] else "NOT RUN YET"))

    # --- 1) is the population finding stable? ------------------------------
    print(f"\n{'-' * 118}\nPOPULATION, per window (wallets with >= {args.min_trades} "
          f"closed trades in that window)\n{'-' * 118}")
    for d in have:
        summarize(list(data[d].values()), f"{d}-day window", args.min_trades)
    print("\n  If the 30-day read was a one-month artifact, these rows disagree. "
          "If it holds, the\n  population of copyable wallets has no edge at any "
          "horizon we can measure.")

    # --- 2) the ranked table ----------------------------------------------
    universe = sorted(set().union(*(set(data[d]) for d in have)))
    merged = []
    for w in universe:
        row = {"wallet": w}
        for d in WINDOWS:
            r = data[d].get(w)
            row[f"roi{d}"] = r["roi"] if r else None
            row[f"n{d}"] = r["n"] if r else 0
            row[f"net{d}"] = r["net"] if r else None
            row[f"dep{d}"] = r["deployed"] if r else None
        any_r = next((data[d][w] for d in reversed(have) if w in data[d]), None)
        row["name"] = any_r.get("name", "") if any_r else ""
        row["span"] = any_r.get("span_days", 0) if any_r else 0
        row["roster"] = bool(any_r and any_r.get("on_roster"))
        rois = [row[f"roi{d}"] for d in have if row[f"roi{d}"] is not None]
        row["windows_scored"] = len(rois)
        row["windows_positive"] = sum(1 for x in rois if x > 0)
        row["worst"] = min(rois) if rois else None
        # How many of the windows are actually DISTINCT readings. A wallet whose
        # visible tape is 13 days long returns the same trades at 30, 60 and 90
        # days, so its three columns are one measurement printed three times.
        counts = [row[f"n{d}"] for d in have if row[f"n{d}"]]
        row["distinct"] = len(set(counts))
        merged.append(row)

    longest = max(have)
    rank_key = f"roi{longest}"
    qualified = [r for r in merged
                 if r[rank_key] is not None and r[f"n{longest}"] >= args.min_trades]
    qualified.sort(key=lambda r: -r[rank_key])

    print(f"\n{'-' * 118}\nRANKED by {longest}-day return, wallets with >= "
          f"{args.min_trades} closed trades in that window ({len(qualified)} qualify)")
    print(f"{'-' * 118}")
    head = (f"  {'#':>3} {'wallet':<14}{'name':<18}"
            + "".join(f"{str(d) + 'd ROI':>10}{str(d) + ' n':>7}" for d in WINDOWS)
            + f"{'+wins':>7}{'tape':>7}")
    print(head)
    for i, r in enumerate(qualified[:args.top], 1):
        cells = ""
        for d in WINDOWS:
            cells += (f"{r[f'roi{d}']:>+9.1f}%" if r[f"roi{d}"] is not None else f"{'--':>10}")
            cells += f"{r[f'n{d}']:>7}" if r[f"n{d}"] else f"{'--':>7}"
        tag = " *" if r["roster"] else ""
        print(f"  {i:>3} {r['wallet'][:12]:<14}{(r['name'] or '')[:17]:<18}{cells}"
              f"{r['windows_positive']}/{r['windows_scored']:>5}{r['span']:>7.0f}{tag}")

    allpos = [r for r in qualified
              if r["windows_scored"] == len(have) and r["windows_positive"] == len(have)]
    print(f"\n  positive in ALL {len(have)} windows: {len(allpos)} of "
          f"{len([r for r in qualified if r['windows_scored'] == len(have)])} "
          f"wallets scored in all of them")
    print("  (a wallet can only clear this by being positive over nested windows, "
          "which is a low bar --\n   the 30-day result is inside the 90-day one, "
          "so these are not three independent votes)")

    if len(have) > 1:
        scored_all = [r for r in merged if r["windows_scored"] == len(have)]
        flat = [r for r in scored_all if r["distinct"] == 1]
        print("")
        print("  HOW MUCH THE LONGER WINDOWS ACTUALLY ADD:")
        share = len(flat) / max(len(scored_all), 1) * 100
        print(f"    {len(flat)} of {len(scored_all)} wallets ({share:.0f}%) return the SAME trade count")
        print("    at every window: their visible tape is shorter than 30 days, so the 60- and")
        print("    90-day columns are the 30-day result reprinted, not new evidence. Check the")
        print("    tape column before reading three votes into three numbers.")
        for d in have[1:]:
            grew = [r for r in scored_all if r["n30"] and r[f"n{d}"] > r["n30"]]
            if grew:
                mult = st.median([r[f"n{d}"] / r["n30"] for r in grew])
                print(f"    at {d}d: {len(grew)} wallets gained trades "
                      f"(median {mult:.1f}x the 30-day count)")

    # --- 3) the honest forward test, on non-overlapping slices -------------
    if 30 in data and 60 in data and 90 in data:
        print(f"\n{'-' * 118}\nNON-OVERLAPPING SLICES -- does the older period predict "
              f"the recent one?\n{'-' * 118}")
        slices = []
        for r in merged:
            if any(r[f"dep{d}"] is None for d in WINDOWS):
                continue
            rec_d, rec_n = r["dep30"], r["net30"]
            mid_d, mid_n = r["dep60"] - r["dep30"], r["net60"] - r["net30"]
            old_d, old_n = r["dep90"] - r["dep60"], r["net90"] - r["net60"]
            if min(rec_d, mid_d, old_d) < 200:      # too thin to be a reading
                continue
            slices.append({
                "wallet": r["wallet"],
                "recent": rec_n / rec_d * 100,
                "middle": mid_n / mid_d * 100,
                "older": old_n / old_d * 100,
            })
        if len(slices) < 10:
            print(f"  only {len(slices)} wallets have a usable slice in all three "
                  f"periods -- too few to read")
        else:
            for a, b in (("older", "middle"), ("middle", "recent"), ("older", "recent")):
                rho = spearman([s[a] for s in slices], [s[b] for s in slices])
                print(f"  corr({a:<6} -> {b:<6}) = {rho:>+6.3f}   n={len(slices)}")
            print("\n  A wallet's own past copy performance is only worth ranking on "
                  "if these are\n  meaningfully positive. Near zero means the same "
                  "thing the roster funnel found.")
            for label in ("older", "middle", "recent"):
                v = [s[label] for s in slices]
                print(f"    {label:<7} mean {st.mean(v):>+7.2f}%  median "
                      f"{st.median(v):>+7.2f}%  positive "
                      f"{sum(1 for x in v if x > 0) / len(v) * 100:>3.0f}%")
        print("\n  Slices are DERIVED by subtracting nested runs, so they are "
              "approximate: a longer\n  run holds different capital at every instant. "
              "Read a strong result here as a reason\n  to run three separate "
              "non-overlapping windows, not as the finding itself.")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(merged, fh, indent=1)
        print(f"\nmerged rows written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
