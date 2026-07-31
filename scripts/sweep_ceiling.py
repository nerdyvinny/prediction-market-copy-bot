"""Sweep the copy entry-price ceiling (PMBOT_COPY_PRICE_MAX).

Live paper trading went negative at a 60% win rate because 7 of 11 entries
landed at 81-87c. At those prices the break-even win rate is 81-87%: you risk
85c to make 15c, so wins are small and losses are near-total. The question is
whether capping entries lower keeps more P&L than it gives up in lost trades.

A single-window parameter sweep would just pick whichever ceiling got lucky,
so every ceiling is scored on TWO independent windows (an older "tune" half
and a recent "validate" half) plus a longer combined window. A ceiling worth
adopting has to look good in all three — if the best value jumps around
between windows, the effect is noise and the honest answer is "leave it".

Secondary: the same tapes re-scored against per-trade size caps, because
$100/trade on a $500 bankroll is 20% of the account on one bet.

Usage:
    python -m scripts.sweep_ceiling
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, PolymarketDataClient

# The live followed set on the VPS (rescored 2026-07-30 20:59 UTC).
LEADERS = [
    "0xd487f513cfead22d76b6db4567c756b3cf25053e",
    "0x41558102a796ba971c7567cad41c307e59f8fa41",
    "0x5f486fdf01685dc2fd8880d0d1fc3495858aba84",
    "0xc3e550fae1c90b71675f3355e5864c240bea519d",
    "0xd2e820929e3940f5e1a8810f3ebfafb25c3dc60e",
    "0x4abdea21c609efca17160f1dcbc652ba498a5a5c",
    "0x3eae57986be5e0ca435102ffe1f14206ffa2e2ed",
]
CEILINGS = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
SIZE_CAPS = [25.0, 50.0, 75.0, 100.0]
SHORT = 21          # tune / validate half-width
LONG = 60           # combined window
TAPE = 5000


def main() -> int:
    now = datetime.now(timezone.utc)
    s = get_settings()
    t0 = time.time()
    data, gamma = PolymarketDataClient(), GammaClient()

    def settings_with(max_per_market: float) -> Settings:
        return Settings(bankroll_usd=s.bankroll_usd, copy_fraction=s.copy_fraction,
                        max_per_market_usd=max_per_market,
                        max_per_leader_usd=s.max_per_leader_usd,
                        compound_profits=False)

    bt = ExactCopyBacktester(data, gamma, settings=settings_with(s.max_per_market_usd),
                             trades_limit=TAPE)
    print(f"=== PRICE-CEILING SWEEP ===  (live ceiling = {s.copy_price_max})", flush=True)
    print(f"fetching {len(LEADERS)} tapes ({TAPE} trades each)…", flush=True)
    tapes = bt.fetch_tapes(LEADERS)
    got = sum(len(v) for v in tapes.values())
    print(f"  {len(tapes)} tapes, {got:,} trades ({time.time()-t0:.0f}s)\n", flush=True)

    def sim(pmax, when, days, sett=None):
        return bt.simulate(tapes, lookback_days=days,
                           min_leader_notional=s.copy_min_leader_notional_usd,
                           price_min=s.copy_price_min, price_max=pmax,
                           now=when, settings=sett).metrics()

    windows = [
        (f"TUNE (older {SHORT}d)", now - timedelta(days=SHORT), SHORT),
        (f"VALIDATE (recent {SHORT}d)", now, SHORT),
        (f"COMBINED ({LONG}d)", now, LONG),
    ]
    best: dict[str, float] = {}
    for label, when, days in windows:
        print(f"--- {label} ---", flush=True)
        print(f"  {'ceiling':>8}{'n':>6}{'net':>11}{'roi':>9}{'maxdd':>10}"
              f"{'avg entry':>11}", flush=True)
        rows = []
        for pmax in CEILINGS:
            m = sim(pmax, when, days)
            rows.append((pmax, m))
            star = " <= live" if abs(pmax - s.copy_price_max) < 1e-9 else ""
            print(f"  {pmax:>8.2f}{m['n_trades']:>6}${m['net_pnl']:>10,.0f}"
                  f"{m['roi']*100:>8.1f}%${m['max_drawdown']:>9,.0f}"
                  f"{m['avg_edge']*100:>10.1f}%{star}", flush=True)
        pick = max(rows, key=lambda r: r[1]["net_pnl"])
        best[label] = pick[0]
        print(f"  best by net P&L: {pick[0]:.2f}\n", flush=True)

    print("=== consistency check ===", flush=True)
    for k, v in best.items():
        print(f"  {k:<26} -> {v:.2f}", flush=True)
    vals = set(best.values())
    print("  VERDICT:", "consistent — a real effect" if len(vals) == 1
          else f"INCONSISTENT across windows ({sorted(vals)}) — treat as noise",
          flush=True)

    print(f"\n=== SECONDARY: per-trade size cap (at live ceiling "
          f"{s.copy_price_max}) ===", flush=True)
    for label, when, days in windows:
        print(f"--- {label} ---", flush=True)
        print(f"  {'cap':>7}{'n':>6}{'net':>11}{'roi':>9}{'maxdd':>10}", flush=True)
        for cap in SIZE_CAPS:
            m = sim(s.copy_price_max, when, days, sett=settings_with(cap))
            star = " <= live" if abs(cap - s.max_per_market_usd) < 1e-9 else ""
            print(f"  ${cap:>6.0f}{m['n_trades']:>6}${m['net_pnl']:>10,.0f}"
                  f"{m['roi']*100:>8.1f}%${m['max_drawdown']:>9,.0f}{star}", flush=True)
    print(f"\n(total {time.time()-t0:.0f}s)", flush=True)
    data.close()
    gamma.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
