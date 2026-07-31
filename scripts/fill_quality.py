"""Measure copy fill quality: our fill price vs the leader's own fill price.

Every entry filter in the strategy judges the LEADER's trade — their price,
their size, their timing. Nothing measures our own execution, so the one
number that explains a gap between a leader's win rate and ours has never
been recorded. This closes that hole.

For each copied BUY we compare:
  - target_price : what the leader paid (the signal's target)
  - fill_price   : what we actually paid

The gap is the premium. Some of it is the modelled slippage budget
(PMBOT_SLIPPAGE_BPS, 60bps by default); the rest is the market moving in the
~10s between their trade and ours. The drift guard caps the total at
`copy_max_price_drift` (0.03), so the effective ceiling is entry-band + 3c.

Why the premium matters more than it looks: at an 82c entry your maximum
profit is 18c. A 2c premium is 11% of the entire prize, and it comes off the
top of every winning trade while doing nothing to soften the losers.

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-backfill", action="store_true")
    args = ap.parse_args()
    s = get_settings()
    db = args.db or s.db_path

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
    has_col = "target_price" in cols

    rows = [dict(r) for r in conn.execute(
        "select ts, side, fill_price, size_usd, slippage_bps, source_leader,"
        f" source_uid{', target_price' if has_col else ''} "
        "from fills where side='BUY' and source_leader is not null order by ts"
    )]
    conn.close()
    if not rows:
        print("no copied BUY fills yet.")
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
    print(f"\n=== COPY FILL QUALITY ===  ({len(usable)}/{len(rows)} buys measurable)")
    if not usable:
        print("No leader prices available. New fills will record them going forward.")
        return 0

    print(f"\n  {'when':<17}{'leader':<14}{'their':>8}{'ours':>8}"
          f"{'premium':>10}{'% of upside':>13}")
    prem_c, prem_frac, upside_burn = [], [], []
    per_leader: dict[str, list[float]] = defaultdict(list)
    for r in usable:
        tp, fp = float(r["target_price"]), float(r["fill_price"])
        d = fp - tp
        # Max profit per share if the bet wins is (1 - entry). The premium
        # eats that prize, so express it as a share of what was winnable.
        upside = max(1.0 - tp, 1e-9)
        prem_c.append(d * 100)
        prem_frac.append(d / tp if tp else 0.0)
        upside_burn.append(d / upside)
        per_leader[r["source_leader"]].append(d * 100)
        print(f"  {r['ts'][:16]:<17}{r['source_leader'][:12]:<14}"
              f"{tp:>8.4f}{fp:>8.4f}{d*100:>+9.2f}c{d/upside*100:>12.1f}%")

    n = len(prem_c)
    print(f"\n  mean premium   : {statistics.mean(prem_c):+.2f}c "
          f"({statistics.mean(prem_frac)*100:+.2f}%)")
    print(f"  median premium : {statistics.median(prem_c):+.2f}c")
    if n > 1:
        print(f"  worst / best   : {max(prem_c):+.2f}c / {min(prem_c):+.2f}c")
    print(f"  modelled budget: {s.slippage_bps:.0f}bps "
          f"(~{statistics.mean([float(r['target_price']) for r in usable])*s.slippage_bps/10_000*100:.2f}c "
          f"at the average entry)")
    print(f"  mean share of winnable upside burned: "
          f"{statistics.mean(upside_burn)*100:.1f}%")

    drift = s.copy_max_price_drift
    over = [r for r in usable
            if float(r["fill_price"]) - float(r["target_price"]) > drift + 1e-9]
    print(f"  fills beyond the {drift:.2f} drift guard: {len(over)}"
          f"{'  <-- guard not holding' if over else ''}")

    print("\n  per leader (mean premium):")
    for w, v in sorted(per_leader.items(), key=lambda kv: -statistics.mean(kv[1])):
        print(f"    {w[:14]}  n={len(v):>3}  {statistics.mean(v):+.2f}c")

    print("\n  NOTE: premium is a cost on winners only — a losing bet loses the\n"
          "  whole stake either way. Judge it against the win rate, not alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
