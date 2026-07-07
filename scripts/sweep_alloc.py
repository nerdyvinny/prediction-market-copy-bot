"""Walk-forward capital-allocation sweep for the exact-copy strategy.

Tunes copy_fraction / per-market cap / per-leader cap / compounding on an
OLDER window (90→45 days ago), then validates the top configurations
out-of-sample on the RECENT 45 days — so the chosen config earned its spot on
data it wasn't fitted to. Also ranks leaders by tune-window copy P&L and
tests following only the top-K, plus a slippage robustness check.

Entry filters are held fixed at the round-1 sweep winners
(min notional $500, price band 0.15–0.85).

Usage:
    python -m scripts.sweep_alloc 0xWALLET [0xWALLET ...]
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import Settings
from pmbot.data import GammaClient, PolymarketDataClient

MIN_NOTIONAL = 500.0
BAND = (0.15, 0.85)
WINDOW_DAYS = 45

COPY_FRACTIONS = [0.05, 0.10, 0.15, 0.20]
MARKET_CAPS = [50.0, 75.0, 100.0]
LEADER_CAPS = [150.0, 250.0, 400.0]
COMPOUND = [True, False]


def _settings(cf, mc, lc, comp):
    return Settings(bankroll_usd=500.0, copy_fraction=cf, max_per_market_usd=mc,
                    max_per_leader_usd=lc, compound_profits=comp)


def _sim(bt, tapes, s, now, slippage_bps=None):
    rep = bt.simulate(tapes, lookback_days=WINDOW_DAYS, min_leader_notional=MIN_NOTIONAL,
                      price_min=BAND[0], price_max=BAND[1], now=now,
                      settings=s, slippage_bps=slippage_bps)
    return rep.metrics()


def main(leaders: list[str]) -> None:
    if not leaders:
        print("usage: python -m scripts.sweep_alloc 0xWALLET [0xWALLET ...]")
        sys.exit(2)

    now = datetime.now(timezone.utc)
    tune_now = now - timedelta(days=WINDOW_DAYS)

    data, gamma = PolymarketDataClient(), GammaClient()
    bt = ExactCopyBacktester(data, gamma, trades_limit=2000)

    t0 = time.time()
    print(f"fetching tapes for {len(leaders)} leaders (limit 2000)…", flush=True)
    tapes = bt.fetch_tapes(leaders)
    for w, tape in tapes.items():
        oldest = min((t.timestamp for t in tape), default=None)
        print(f"  {w[:12]}… {len(tape)} trades (back to {oldest:%Y-%m-%d})" if oldest
              else f"  {w[:12]}… 0 trades", flush=True)

    # --- 1) allocation grid on the TUNE window (45-90 days ago) ---------
    rows = []
    for cf in COPY_FRACTIONS:
        for mc in MARKET_CAPS:
            for lc in LEADER_CAPS:
                for comp in COMPOUND:
                    m = _sim(bt, tapes, _settings(cf, mc, lc, comp), tune_now)
                    rows.append(((cf, mc, lc, comp), m))
    rows.sort(key=lambda r: -r[1]["net_pnl"])

    print(f"\n=== TUNE window ({WINDOW_DAYS}d ending {tune_now:%Y-%m-%d}) — top 12 of {len(rows)} ===")
    print(f"{'frac':>5} {'mkt$':>5} {'ldr$':>5} {'comp':>5} {'n':>4} {'net_pnl':>9} {'roi%':>6} {'mdd':>8}")
    for (cf, mc, lc, comp), m in rows[:12]:
        print(f"{cf:>5.2f} {mc:>5.0f} {lc:>5.0f} {str(comp):>5} {m['n_trades']:>4} "
              f"${m['net_pnl']:>8,.2f} {m['roi']*100:>5.1f} ${m['max_drawdown']:>7,.2f}")

    # --- 2) validate the top-5 tune configs out-of-sample ----------------
    baseline = ((0.05, 50.0, 150.0, False), None)
    print(f"\n=== VALIDATE window (last {WINDOW_DAYS}d) — top-5 tune configs + round-1 baseline ===")
    print(f"{'frac':>5} {'mkt$':>5} {'ldr$':>5} {'comp':>5} {'n':>4} {'net_pnl':>9} {'roi%':>6} {'mdd':>8}")
    best_val = None
    for (cf, mc, lc, comp), _ in rows[:5] + [baseline]:
        m = _sim(bt, tapes, _settings(cf, mc, lc, comp), now)
        tag = " <- baseline" if (cf, mc, lc, comp) == baseline[0] else ""
        print(f"{cf:>5.2f} {mc:>5.0f} {lc:>5.0f} {str(comp):>5} {m['n_trades']:>4} "
              f"${m['net_pnl']:>8,.2f} {m['roi']*100:>5.1f} ${m['max_drawdown']:>7,.2f}{tag}")
        if not tag and (best_val is None or m["net_pnl"] > best_val[1]["net_pnl"]):
            best_val = ((cf, mc, lc, comp), m)

    cf, mc, lc, comp = best_val[0]
    chosen = _settings(cf, mc, lc, comp)
    print(f"\nchosen config: frac={cf} mkt_cap=${mc:.0f} ldr_cap=${lc:.0f} compound={comp}")

    # --- 3) leader subset: rank by tune-window P&L, validate top-K -------
    per_leader = []
    for w in tapes:
        m = _sim(bt, {w: tapes[w]}, chosen, tune_now)
        per_leader.append((w, m["net_pnl"], m["n_trades"]))
    per_leader.sort(key=lambda x: -x[1])
    print("\n=== per-leader tune-window copy P&L (chosen config) ===")
    for w, pnl, n in per_leader:
        print(f"  {w[:12]}…  {n:>3} trades  net ${pnl:,.2f}")

    print(f"\n=== VALIDATE window: follow only top-K tune leaders ===")
    for k in (3, 5, len(per_leader)):
        subset = {w: tapes[w] for w, _, _ in per_leader[:k]}
        m = _sim(bt, subset, chosen, now)
        print(f"  top-{k:<2}  n={m['n_trades']:>4} net=${m['net_pnl']:>8,.2f} "
              f"roi={m['roi']*100:>5.1f}% mdd=${m['max_drawdown']:>7,.2f}")

    # --- 4) slippage robustness on the validate window -------------------
    print("\n=== slippage robustness (validate window, all leaders, chosen config) ===")
    for bps in (60, 100, 150):
        m = _sim(bt, tapes, chosen, now, slippage_bps=bps)
        print(f"  {bps:>3}bps  net=${m['net_pnl']:>8,.2f} roi={m['roi']*100:>5.1f}%")

    print(f"\n(total {time.time()-t0:.0f}s)")
    data.close()
    gamma.close()


if __name__ == "__main__":
    main([w.lower() for w in sys.argv[1:]])
