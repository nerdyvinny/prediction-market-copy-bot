"""Walk-forward vet of the wallets `min_win_rate` excludes.

The deep sweep's near-miss report showed 19 wallets that fail ONLY the 0.80
win-rate filter, several with six- and seven-figure 30-day P&L at 46-60% win
rates. That is a coherent trader profile, not noise: positive expectancy from
payoff asymmetry (buying cheap longshots) rather than from winning often. The
live filter excludes the entire class by construction.

Whether they are *copyable* is a separate question from whether they are
profitable — big longshot bets can be unfillable at our size, and a 48% win
rate means long losing streaks the bankroll has to survive. So this script
answers it the only way that counts: exact-copy backtest, walk-forward.

Windows are deliberately shorter than the deep sweep's 45/45. These wallets
trade hundreds of times a month, so even a 5000-trade tape does not reach 90
days back; a 21/21 split is what the tape actually covers.

Usage:
    python -m scripts.vet_lowwr
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import Settings, get_settings
from pmbot.data import GammaClient, PolymarketDataClient
from scripts._book import shared_book

WINDOW = 21              # tune = [now-2W, now-W], validate = [now-W, now]
TAPE = 5000              # these wallets burn 1000 trades in days
TOP_N = 12               # low-win-rate candidates to vet


def main() -> int:
    now = datetime.now(timezone.utc)
    s = get_settings()
    t0 = time.time()

    d = json.load(open("deep_sweep_results.json"))
    incumbents = [w.lower() for w in d["incumbents"]]
    lowwr = [x for x in d["near_miss"] if x["failed"] == "win_rate"]
    lowwr.sort(key=lambda x: -x["realized_pnl"])
    cands = [x["wallet"] for x in lowwr[:TOP_N]]

    print(f"=== LOW-WIN-RATE VET ({WINDOW}d tune / {WINDOW}d validate) ===", flush=True)
    print(f"candidates: {len(cands)}   incumbents: {len(incumbents)}", flush=True)
    for x in lowwr[:TOP_N]:
        print(f"  {x['wallet'][:14]} pnl=${x['realized_pnl']:>10,.0f} "
              f"wr={x['win_rate']*100:>5.1f}% res={x['n_resolved_markets']:>4} "
              f"cpy={x['n_copyable_trades']:>4}", flush=True)

    data, gamma = PolymarketDataClient(), GammaClient()
    vs = Settings(bankroll_usd=s.bankroll_usd, copy_fraction=s.copy_fraction,
                  max_per_market_usd=s.max_per_market_usd,
                  max_per_leader_usd=s.max_per_leader_usd, compound_profits=False)
    bt = ExactCopyBacktester(data, gamma, settings=vs, trades_limit=TAPE)

    print(f"\nfetching {len(cands)+len(incumbents)} deep tapes ({TAPE} trades)…", flush=True)
    tapes = bt.fetch_tapes(cands + incumbents)
    # Quote the book once for the whole tape: `simulate` prices every fill
    # off it and skips what it cannot quote, exactly as the live executor
    # does. Without this the run fills at the leader's own price.
    shared_book(bt, tapes)
    print(f"  got {len(tapes)} ({time.time()-t0:.0f}s)", flush=True)

    def sim(sub, when):
        return bt.simulate(sub, lookback_days=WINDOW,
                           min_leader_notional=s.copy_min_leader_notional_usd,
                           price_min=s.copy_price_min, price_max=s.copy_price_max,
                           now=when).metrics()

    tune_now = now - timedelta(days=WINDOW)
    print(f"\n{'wallet':<16}{'tune n':>7}{'tune pnl':>11}{'val n':>7}"
          f"{'val pnl':>11}{'val roi':>9}{'val mdd':>10}", flush=True)
    tune, val = {}, {}
    for w in tapes:
        tune[w], val[w] = sim({w: tapes[w]}, tune_now), sim({w: tapes[w]}, now)
        mark = " *held" if w in incumbents else ""
        print(f"  {w[:14]}{tune[w]['n_trades']:>7}${tune[w]['net_pnl']:>10,.0f}"
              f"{val[w]['n_trades']:>7}${val[w]['net_pnl']:>10,.0f}"
              f"{val[w]['roi']*100:>8.1f}%${val[w]['max_drawdown']:>9,.0f}{mark}", flush=True)

    held = [w for w in incumbents if w in tapes]
    # Only candidates that were profitable in the TUNE half earn a slot — the
    # validate half must stay honestly out-of-sample.
    good = [w for w in cands
            if w in tune and tune[w]["n_trades"] >= 3 and tune[w]["net_pnl"] > 0]
    good.sort(key=lambda w: -tune[w]["net_pnl"])
    portfolios = {
        "incumbents only": held,
        "low-wr only (tune+)": good,
        "incumbents + top-2 low-wr": held + good[:2],
        "incumbents + top-4 low-wr": held + good[:4],
        "incumbents + all low-wr": held + good,
    }
    print(f"\n=== OUT-OF-SAMPLE portfolios (last {WINDOW}d) ===", flush=True)
    print(f"  {'portfolio':<28}{'n':>5}{'net':>11}{'roi':>9}{'maxdd':>10}", flush=True)
    for name, members in portfolios.items():
        sub = {w: tapes[w] for w in dict.fromkeys(members) if w in tapes}
        if not sub:
            print(f"  {name:<28}  (empty)", flush=True)
            continue
        m = sim(sub, now)
        print(f"  {name:<28}{m['n_trades']:>5}${m['net_pnl']:>10,.0f}"
              f"{m['roi']*100:>8.1f}%${m['max_drawdown']:>9,.0f}", flush=True)
    print(f"\n(tune-positive low-wr candidates: {len(good)}/{len(cands)}; "
          f"total {time.time()-t0:.0f}s)", flush=True)
    data.close()
    gamma.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
