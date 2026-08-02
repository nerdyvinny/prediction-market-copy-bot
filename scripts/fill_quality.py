"""Measure copy fill quality: our fill price vs the leader's own fill price.

Every entry filter in the strategy judges the LEADER's trade — their price,
their size, their timing. Nothing measures our own execution, so the one
number that explains a gap between a leader's win rate and ours has never
been recorded. This closes that hole.

Both legs are measured, separately, because they are governed differently:

  ENTRIES (BUY)  are protected. `_price_drift_ok` refuses any entry whose
                 quote has moved more than `copy_max_price_drift` (0.03) from
                 the leader's fill, so the adverse gap has a hard ceiling.
  EXITS (SELL)   are NOT protected, deliberately: mirroring a leader out of a
                 position reduces risk, and blocking an exit is worse than a
                 poor price on one. The consequence is that an exit can fill
                 arbitrarily far from the leader once the market has moved.

That asymmetry is the reason to split them. The first 8 fills after the
2026-08-01 floor change had entries averaging ~1c adverse but exits ~4c, one
of them 9.2c (leader sold 0.4800, we got 0.3877) — a structural cost that a
BUY-only measurement cannot see, and that the backtester cannot see either
(it fills both legs at the leader's price ± a flat 60bps).

Sign convention: **positive is always worse for us.**
  BUY  adverse = fill - target   (we paid more than they did)
  SELL adverse = target - fill   (we received less than they did)

Why the gap matters more than it looks: at an 82c entry your maximum profit is
18c. A 2c premium is 11% of the entire prize, and it comes off the top of every
winning trade while doing nothing to soften the losers.

Rows written before the `target_price` column existed are backfilled from the
API: fills store `source_uid`, which is the leader's transaction hash, so the
leader's tape can be re-fetched and matched exactly.

Usage:
    python -m scripts.fill_quality                 # local pmbot.db
    python -m scripts.fill_quality --db /path.db
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict

from pmbot.config import get_settings
from pmbot.data import PolymarketDataClient


def adverse(side: str, target: float, fill: float) -> float:
    """Price moved against us, in absolute price units. Positive = worse."""
    return (fill - target) if side.upper() == "BUY" else (target - fill)


def _leg_report(rows: list[dict], side: str, s) -> tuple[float, float] | None:
    """Print one leg's table + stats. Returns (mean_cents, total_usd_cost)."""
    if not rows:
        print(f"\n--- {side} --- none yet")
        return None

    label = "premium paid" if side == "BUY" else "shortfall"
    print(f"\n--- {side}  ({len(rows)} measurable) ---")
    print(f"  {'when':<17}{'leader':<14}{'their':>8}{'ours':>8}"
          f"{'adverse':>10}{'$ cost':>9}")

    cents, costs = [], []
    per_leader: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        tp, fp = float(r["target_price"]), float(r["fill_price"])
        d = adverse(side, tp, fp)
        # Adverse price x shares = the dollars this leg handed to the market.
        cost = d * float(r["shares"] or 0)
        cents.append(d * 100)
        costs.append(cost)
        per_leader[r["source_leader"]].append(d * 100)
        print(f"  {r['ts'][:16]:<17}{r['source_leader'][:12]:<14}"
              f"{tp:>8.4f}{fp:>8.4f}{d*100:>+9.2f}c{cost:>+9.2f}")

    mean_c = statistics.mean(cents)
    print(f"\n  mean {label:<14}: {mean_c:+.2f}c")
    print(f"  median              : {statistics.median(cents):+.2f}c")
    if len(cents) > 1:
        print(f"  worst / best        : {max(cents):+.2f}c / {min(cents):+.2f}c")
    print(f"  worse than leader   : {sum(1 for c in cents if c > 0)}/{len(cents)}")
    print(f"  total $ handed away : {sum(costs):+.2f}")

    if side == "BUY":
        upside = [
            adverse("BUY", float(r["target_price"]), float(r["fill_price"]))
            / max(1.0 - float(r["target_price"]), 1e-9)
            for r in rows
        ]
        print(f"  share of winnable upside burned: {statistics.mean(upside)*100:.1f}%")
        drift = s.copy_max_price_drift
        over = [r for r in rows
                if adverse("BUY", float(r["target_price"]), float(r["fill_price"]))
                > drift + 1e-9]
        print(f"  beyond the {drift:.2f} drift guard: {len(over)}"
              f"{'  <-- guard not holding' if over else ''}")
    else:
        print("  NOTE: no drift guard on this path by design — an exit is never\n"
              "        blocked, so nothing bounds how far these can run.")

    print(f"\n  per leader (mean adverse, {side}):")
    for w, v in sorted(per_leader.items(), key=lambda kv: -statistics.mean(kv[1])):
        print(f"    {w[:14]}  n={len(v):>3}  {statistics.mean(v):+.2f}c")

    return mean_c, sum(costs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--since", default=None,
                    help="ISO timestamp; only fills at or after it (e.g. the "
                         "moment a parameter changed)")
    args = ap.parse_args()
    s = get_settings()
    db = args.db or s.db_path

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
    has_col = "target_price" in cols

    # `reason like 'copy%'` keeps settlements out: those carry no leader price
    # and would dilute the very number we are trying to isolate.
    sql = (
        "select ts, side, fill_price, size_usd, shares, slippage_bps, "
        f"source_leader, source_uid{', target_price' if has_col else ''} "
        "from fills where source_leader is not null and reason like 'copy%'"
    )
    params: tuple = ()
    if args.since:
        sql += " and ts >= ?"
        params = (args.since,)
    rows = [dict(r) for r in conn.execute(sql + " order by ts", params)]
    conn.close()
    if not rows:
        print("no copied fills yet.")
        return 0

    # Backfill the leader's price from their tape (source_uid == tx hash).
    missing = [r for r in rows if not r.get("target_price")]
    if missing and not args.no_backfill:
        print(f"backfilling {len(missing)} rows from the API…", flush=True)
        data = PolymarketDataClient()
        by_leader: dict[str, list[dict]] = defaultdict(list)
        for r in missing:
            by_leader[r["source_leader"]].append(r)
        for leader, rs in by_leader.items():
            price_of: dict[str, float] = {}
            try:
                for off in (0, 500, 1000):
                    for t in data.get_trades(user=leader, limit=500, offset=off):
                        price_of[t.uid] = t.price
            except Exception as e:
                print(f"  {leader[:12]}: fetch failed ({type(e).__name__})")
                continue
            hit = 0
            for r in rs:
                p = price_of.get(r["source_uid"] or "")
                if p:
                    r["target_price"] = p
                    hit += 1
            print(f"  {leader[:12]}: matched {hit}/{len(rs)}")
        data.close()

    usable = [r for r in rows if r.get("target_price")]
    scope = f" since {args.since}" if args.since else ""
    print(f"\n=== COPY FILL QUALITY ==={scope}"
          f"  ({len(usable)}/{len(rows)} fills measurable)")
    if not usable:
        print("No leader prices available. New fills will record them going forward.")
        return 0

    buys = [r for r in usable if (r["side"] or "").upper() == "BUY"]
    sells = [r for r in usable if (r["side"] or "").upper() == "SELL"]

    buy_stats = _leg_report(buys, "BUY", s)
    sell_stats = _leg_report(sells, "SELL", s)

    # The comparison this script exists for.
    print("\n=== WHERE THE COST IS ===")
    if buy_stats:
        print(f"  entries : {buy_stats[0]:+.2f}c mean over {len(buys):>3} fills"
              f"   ${buy_stats[1]:+.2f} total")
    if sell_stats:
        print(f"  exits   : {sell_stats[0]:+.2f}c mean over {len(sells):>3} fills"
              f"   ${sell_stats[1]:+.2f} total")
    if buy_stats and sell_stats:
        be, se = buy_stats[0], sell_stats[0]
        if se > be > 0:
            print(f"\n  Exits are running {se/max(be,1e-9):.1f}x worse than entries.")
            print("  Entries are capped by the drift guard; exits are not. If this\n"
                  "  holds up over more fills, the fix belongs on the exit path\n"
                  "  (drift-aware or faster mirroring) — NOT another notional sweep,\n"
                  "  and the backtester cannot show it either way.")
        elif be > se:
            print("\n  Entries are the costlier leg — the drift guard may be too loose.")
        modelled = statistics.mean(
            [float(r["target_price"]) for r in usable]) * s.slippage_bps / 10_000 * 100
        print(f"\n  modelled slippage budget: {s.slippage_bps:.0f}bps "
              f"(~{modelled:.2f}c at the average price)")
        print(f"  total handed to the market: ${buy_stats[1] + sell_stats[1]:+.2f}")

    print("\n  NOTE: on entries the premium is a cost on winners only — a losing bet\n"
          "  loses the stake either way. On exits it is a cost on every trade.\n"
          "  Judge against the win rate, not alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
