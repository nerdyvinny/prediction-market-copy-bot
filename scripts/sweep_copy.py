"""Parameter sweep for ExactCopyBacktester over real leader tapes.

Fetches each leader's trade tape once, then simulates every combination of
lookback window, minimum leader notional, and entry price band offline
(market resolutions are memoized after the first pass). Prints a ranked
table plus a per-leader breakdown of the best configuration.

Usage:
    python -m scripts.sweep_copy 0xWALLET [0xWALLET ...]
"""

from __future__ import annotations

import sys
import time

from pmbot.backtest import ExactCopyBacktester
from pmbot.data import GammaClient, PolymarketDataClient

LOOKBACKS = [30, 45, 60, 90]
MIN_NOTIONALS = [0.0, 100.0, 500.0]
PRICE_BANDS = [(0.05, 0.95), (0.10, 0.90), (0.15, 0.85)]


def main(leaders: list[str]) -> None:
    if not leaders:
        print("usage: python -m scripts.sweep_copy 0xWALLET [0xWALLET ...]")
        sys.exit(2)

    data, gamma = PolymarketDataClient(), GammaClient()
    bt = ExactCopyBacktester(data, gamma, trades_limit=1000)

    t0 = time.time()
    print(f"fetching tapes for {len(leaders)} leaders…", flush=True)
    tapes = bt.fetch_tapes(leaders)
    for w, tape in tapes.items():
        print(f"  {w[:12]}… {len(tape)} trades", flush=True)

    rows = []
    for lb in LOOKBACKS:
        for mn in MIN_NOTIONALS:
            for p_min, p_max in PRICE_BANDS:
                rep = bt.simulate(tapes, lookback_days=lb, min_leader_notional=mn,
                                  price_min=p_min, price_max=p_max)
                m = rep.metrics()
                rows.append((lb, mn, p_min, p_max, m))
                print(f"  done lb={lb} mn={mn} band={p_min}-{p_max} "
                      f"n={m['n_trades']} pnl=${m['net_pnl']:.2f}", flush=True)

    print(f"\n(fetch+sweep took {time.time()-t0:.0f}s)")
    print(f"\n{'look':>4} {'minNot':>6} {'band':>10} {'n':>4} {'invested':>10} "
          f"{'net_pnl':>9} {'roi%':>6} {'win%':>5} {'mdd':>8}")
    for lb, mn, p_min, p_max, m in sorted(rows, key=lambda r: -r[4]["net_pnl"]):
        print(f"{lb:>4} {mn:>6.0f} {p_min:>4.2f}-{p_max:<4.2f} {m['n_trades']:>4} "
              f"${m['invested']:>9,.2f} ${m['net_pnl']:>8,.2f} {m['roi']*100:>5.1f} "
              f"{m['win_rate']*100:>5.1f} ${m['max_drawdown']:>7,.2f}")

    best = max(rows, key=lambda r: r[4]["net_pnl"])
    lb, mn, p_min, p_max, _ = best
    rep = bt.simulate(tapes, lookback_days=lb, min_leader_notional=mn,
                      price_min=p_min, price_max=p_max)
    print(f"\nBest config: lookback={lb}d min_notional=${mn:.0f} band={p_min}-{p_max}")
    print(rep.summary_text())

    data.close()
    gamma.close()


if __name__ == "__main__":
    main([w.lower() for w in sys.argv[1:]])
