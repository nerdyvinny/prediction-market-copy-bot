"""Sweep the two filters that actually block copies: the entry-price ceiling
and the leader-notional floor.

Measured 2026-08-13 by replaying the live poll loop over the followed set, the
copy funnel looks like this (one week of tape, per leader):

    leader   buys   killed by band   killed by $150 floor   copyable
    d487      272        268                   2                2
    c23d      388        291                  47               50
    5cd5      449        377                  30               42
    e839      248         30                 100               30
    88f9      245         23                 112               24
    bb3e      242         32                 125               22

Two populations, two different walls. d487/c23d/5cd5 buy heavy favourites in
size — d487's last twelve entries were all 0.94-0.99 at $200-900 each, and the
0.85 ceiling refuses every one. e839/88f9/bb3e scalp $1-25 tickets, and the
$150 floor refuses every one. The round-trip rule, which an earlier pass
blamed, kills nothing on the first three and is not the question here.

`scripts/sweep_ceiling.py` already asked half of this and stopped at 0.90,
where it found the best value jumping between windows and called it noise. It
never tested the range the leaders actually trade in.

Every setting is scored on two disjoint windows (an older TUNE half and a
recent VALIDATE half) plus the combined span. A setting worth adopting has to
hold up in both halves; if the winner moves between them, it is noise.

Usage:
    python -m scripts.sweep_entry_filters
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, PolymarketDataClient

# The live followed set (rescored 2026-08-12 21:16 UTC) plus 0x5cd5c8d7, which
# vetting dropped that day and the allowlist has since pinned back.
#
# SURVIVORSHIP: this cohort was selected and vetted on overlapping data, so the
# absolute P&L here is optimistic and is NOT a forecast. The comparison across
# settings is still fair — the same tapes are scored every time, and the entry
# band never entered leader selection above 0.85, so the trades a higher
# ceiling admits are ones no filter ever picked these wallets for.
LEADERS = [
    "0xd487f513cfead22d76b6db4567c756b3cf25053e",
    "0xc23dc0eca9e1c2e293de8911b9ac254f0bcd82c8",
    "0xe839e7fe9cbd0997200c83d0fb77e7c290a10a9d",
    "0x88f9a9b2878aa55d7763543fc7255d216c555c03",
    "0xbb3eaeb82973aa8f140ae596f792fcdbd9cab9c5",
    "0x5cd5c8d7a17c78d7389d8b87b611aed83322ac33",
]
CEILINGS = [0.85, 0.90, 0.93, 0.95, 0.97, 0.99]
FLOORS = [150.0, 100.0, 50.0, 25.0]
SHORT = 21          # tune / validate half-width, days
LONG = 60           # combined window, days
TAPE = 5000


def _run(bt, tapes, *, now, days, ceiling, floor, s):
    return bt.simulate(
        tapes, lookback_days=days, now=now,
        price_min=s.copy_price_min, price_max=ceiling,
        min_leader_notional=floor,
        skip_round_tripped_entries=s.skip_round_tripped_entries,
    ).metrics()


# A "winner" inside this margin of the runner-up is a tie, not a preference.
# The first run of this sweep reported the notional floor as CONSISTENT across
# both halves — but all four floors scored -$439 in the tune window, differing
# by cents, and an unguarded `>` turned that rounding noise into a verdict.
_TIE_USD = 25.0


def _table(title, rows, header, live_key):
    print(f"--- {title} ---")
    print(f"  {header:>8} {'n':>5} {'net':>10} {'roi':>8} {'win':>7} {'maxdd':>9}")
    for key, m in rows:
        mark = "  <= live" if key == live_key else ""
        print(f"  {key:>8} {m['n_trades']:>5} ${m['net_pnl']:>9,.0f} "
              f"{m['roi']*100:>7.1f}% {m['win_rate']*100:>6.1f}% "
              f"${m['max_drawdown']:>8,.0f}{mark}")
    ordered = sorted(rows, key=lambda kv: -kv[1]["net_pnl"])
    best, best_m = ordered[0]
    margin = best_m["net_pnl"] - ordered[1][1]["net_pnl"] if len(ordered) > 1 else 0.0
    if margin < _TIE_USD:
        tied = [k for k, m in ordered if best_m["net_pnl"] - m["net_pnl"] < _TIE_USD]
        print(f"  best by net P&L: TIE within ${_TIE_USD:.0f} ({', '.join(tied)})\n")
        return None
    print(f"  best by net P&L: {best}  (by ${margin:,.0f})\n")
    return best


def main() -> int:
    now = datetime.now(timezone.utc)
    s = get_settings()
    t0 = time.time()
    data, gamma = PolymarketDataClient(), GammaClient()
    bt = ExactCopyBacktester(data, gamma, settings=s, trades_limit=TAPE)

    print(f"bankroll ${s.bankroll_usd:.0f} | cap ${s.max_per_market_usd:.0f}/market "
          f"| fraction {s.copy_fraction} | slippage {s.slippage_bps:.0f}bps")
    print(f"live ceiling {s.copy_price_max} | live floor "
          f"${s.copy_min_leader_notional_usd:.0f}\n")
    print(f"fetching {len(LEADERS)} tapes (limit {TAPE})…")
    tapes = bt.fetch_tapes(LEADERS)
    for w, tape in tapes.items():
        if tape:
            span = (max(t.timestamp for t in tape)
                    - min(t.timestamp for t in tape)).total_seconds() / 86400
            print(f"  {w[:10]}  {len(tape):>5} trades over {span:>5.1f}d")
    print()

    windows = [
        ("TUNE (older 21d)", SHORT, now - timedelta(days=SHORT)),
        ("VALIDATE (recent 21d)", SHORT, now),
        (f"COMBINED ({LONG}d)", LONG, now),
    ]

    print("=" * 62)
    print("PRIMARY: entry-price ceiling (at the live $150 notional floor)")
    print("=" * 62)
    picks = []
    for title, days, end in windows:
        rows = [(f"{c:.2f}", _run(bt, tapes, now=end, days=days, ceiling=c,
                                  floor=s.copy_min_leader_notional_usd, s=s))
                for c in CEILINGS]
        picks.append(_table(title, rows, "ceiling", f"{s.copy_price_max:.2f}"))
    print("=== consistency check ===")
    for (title, _, _), p in zip(windows, picks):
        print(f"  {title:<26} -> {p}")
    agree = picks[0] == picks[1]
    print(f"  VERDICT: {'CONSISTENT' if agree else 'INCONSISTENT'} across the two "
          f"halves ({sorted(set(picks[:2]))})"
          f"{'' if agree else ' — treat as noise'}\n")

    print("=" * 62)
    print("SECONDARY: leader-notional floor (at the live 0.85 ceiling)")
    print("=" * 62)
    fpicks = []
    for title, days, end in windows:
        rows = [(f"${f:.0f}", _run(bt, tapes, now=end, days=days,
                                   ceiling=s.copy_price_max, floor=f, s=s))
                for f in FLOORS]
        fpicks.append(_table(title, rows, "floor",
                             f"${s.copy_min_leader_notional_usd:.0f}"))
    print("=== consistency check ===")
    for (title, _, _), p in zip(windows, fpicks):
        print(f"  {title:<26} -> {p}")
    fagree = fpicks[0] == fpicks[1]
    print(f"  VERDICT: {'CONSISTENT' if fagree else 'INCONSISTENT'} across the two "
          f"halves ({sorted(set(fpicks[:2]))})"
          f"{'' if fagree else ' — treat as noise'}")
    print("  NB the backtest fills BOTH legs of a round-trip at the leader's own")
    print("  price, so it books fast scalps as winners when they lose live. A")
    print("  lower floor admits mostly scalps, so any gain shown here is")
    print("  overstated by an unknown amount. Treat a win here as unproven.\n")

    print("=" * 62)
    print("PER-LEADER at the live ceiling vs the best consistent one")
    print("=" * 62)
    winner = picks[1] if not agree else picks[0]
    for label, ceil in ((f"live {s.copy_price_max:.2f}", s.copy_price_max),
                        (f"candidate {winner}", float(winner))):
        m = _run(bt, tapes, now=now, days=LONG, ceiling=ceil,
                 floor=s.copy_min_leader_notional_usd, s=s)
        print(f"--- {label}: {m['n_trades']} trades, ${m['net_pnl']:,.0f} net, "
              f"{m['roi']*100:.1f}% ROI ---")
        for w, (n, pnl) in sorted(m["by_leader"].items(), key=lambda kv: -kv[1][1]):
            print(f"    {w[:10]}  {n:>4} trades  ${pnl:>9,.0f}")
        print()

    data.close()
    gamma.close()
    print(f"(total {time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
