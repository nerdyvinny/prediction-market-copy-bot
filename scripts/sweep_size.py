"""Backtest the per-trade risk cap (PMBOT_MAX_PER_MARKET_USD): $100 -> $50.

The ceiling sweep already showed ROI is flat across size caps — size is a
leverage dial, not an edge dial. So total P&L is the LEAST interesting output
here and drawdown is the most: at $100/trade the 60d peak-to-trough swing was
$565 against a $500 bankroll, survivable in that run only because it arrived
after gains.

"Survivable because of when it happened" is not a property you can count on,
so this script adds the test the sweep lacked: ROLLING STARTS. It replays the
same tape from many different starting points and asks how the account would
have fared if the bad stretch had come first. Ruin is path-dependent — one
ordering of the same trades ends at +$8k, another ends at zero — and only a
rolling test exposes that.

Usage:
    python -m scripts.sweep_size
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, PolymarketDataClient

LEADERS = [
    "0xd487f513cfead22d76b6db4567c756b3cf25053e",
    "0x41558102a796ba971c7567cad41c307e59f8fa41",
    "0x5f486fdf01685dc2fd8880d0d1fc3495858aba84",
    "0xc3e550fae1c90b71675f3355e5864c240bea519d",
    "0xd2e820929e3940f5e1a8810f3ebfafb25c3dc60e",
    "0x4abdea21c609efca17160f1dcbc652ba498a5a5c",
    "0x3eae57986be5e0ca435102ffe1f14206ffa2e2ed",
]
CAPS = [25.0, 50.0, 75.0, 100.0]
LONG = 60
ROLL_WINDOW = 14        # each rolling start replays this many days
ROLL_STEPS = 12         # how many staggered start points to test
TAPE = 5000


def main() -> int:
    now = datetime.now(timezone.utc)
    s = get_settings()
    t0 = time.time()
    data, gamma = PolymarketDataClient(), GammaClient()

    def sett(cap: float) -> Settings:
        return Settings(bankroll_usd=s.bankroll_usd, copy_fraction=s.copy_fraction,
                        max_per_market_usd=cap, max_per_leader_usd=s.max_per_leader_usd,
                        compound_profits=False)

    bt = ExactCopyBacktester(data, gamma, settings=sett(100.0), trades_limit=TAPE)
    print(f"=== PER-TRADE SIZE CAP: $100 -> $50 ===", flush=True)
    print(f"bankroll ${s.bankroll_usd:,.0f} | live cap ${s.max_per_market_usd:,.0f}",
          flush=True)
    print(f"fetching {len(LEADERS)} tapes…", flush=True)
    tapes = bt.fetch_tapes(LEADERS)
    print(f"  {sum(len(v) for v in tapes.values()):,} trades ({time.time()-t0:.0f}s)\n",
          flush=True)

    def sim(cap, when, days):
        return bt.simulate(tapes, lookback_days=days,
                           min_leader_notional=s.copy_min_leader_notional_usd,
                           price_min=s.copy_price_min, price_max=s.copy_price_max,
                           now=when, settings=sett(cap)).metrics()

    print(f"--- headline ({LONG}d) ---", flush=True)
    print(f"  {'cap':>6}{'n':>6}{'net':>10}{'roi':>8}{'maxdd':>9}"
          f"{'dd/bankroll':>13}", flush=True)
    base = {}
    for cap in CAPS:
        m = sim(cap, now, LONG)
        base[cap] = m
        star = " <= live" if abs(cap - s.max_per_market_usd) < 1e-9 else ""
        print(f"  ${cap:>5.0f}{m['n_trades']:>6}${m['net_pnl']:>9,.0f}"
              f"{m['roi']*100:>7.1f}%${m['max_drawdown']:>8,.0f}"
              f"{m['max_drawdown']/s.bankroll_usd*100:>12.0f}%{star}", flush=True)

    if 100.0 in base and 50.0 in base:
        a, b = base[100.0], base[50.0]
        print(f"\n  $100 -> $50 : net ${a['net_pnl']:,.0f} -> ${b['net_pnl']:,.0f} "
              f"({(b['net_pnl']/a['net_pnl']-1)*100:+.0f}%), "
              f"drawdown ${a['max_drawdown']:,.0f} -> ${b['max_drawdown']:,.0f} "
              f"({(b['max_drawdown']/a['max_drawdown']-1)*100:+.0f}%)", flush=True)

    # --- rolling starts: does the ORDER of trades matter? ------------------
    print(f"\n--- rolling starts ({ROLL_STEPS} x {ROLL_WINDOW}d windows) ---",
          flush=True)
    print("  worst-case behaviour when the bad stretch lands FIRST", flush=True)
    print(f"  {'cap':>6}{'wins':>7}{'median':>10}{'worst':>10}"
          f"{'worst dd':>11}{'ruin':>7}", flush=True)
    for cap in CAPS:
        nets, dds, ruined = [], [], 0
        for k in range(ROLL_STEPS):
            when = now - timedelta(days=k * (LONG - ROLL_WINDOW) / max(ROLL_STEPS - 1, 1))
            m = sim(cap, when, ROLL_WINDOW)
            if m["n_trades"] == 0:
                continue
            nets.append(m["net_pnl"])
            dds.append(m["max_drawdown"])
            # Ruin proxy: a drawdown at or beyond the whole bankroll means the
            # account would have been wiped had this stretch come first.
            if m["max_drawdown"] >= s.bankroll_usd:
                ruined += 1
        if not nets:
            continue
        star = " <= live" if abs(cap - s.max_per_market_usd) < 1e-9 else ""
        print(f"  ${cap:>5.0f}{sum(1 for n in nets if n>0):>4}/{len(nets):<3}"
              f"${statistics.median(nets):>9,.0f}${min(nets):>9,.0f}"
              f"${max(dds):>10,.0f}{ruined:>4}/{len(nets)}{star}", flush=True)

    print(f"\n(total {time.time()-t0:.0f}s)", flush=True)
    data.close()
    gamma.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
