"""Compare pmbot's leader selection vs a Fomo-style 'top winrate + efficiency' pick.

Fomo (the social copy-trading app) surfaces leaders via curated stat collections
("90%+ Winrate", "High-Efficiency") with no copy-backtest vetting. This script
asks: on the SAME candidate pool and the SAME 45d window, does that selection
rule beat pmbot's (filters -> weighted rank -> copy-vet)?

Method (one network pass, all comparisons offline on identical tapes):
  1. Harvest the pool exactly like the live engine (top 30 markets x 100
     wallets, alphabetical cap 200 — LeaderSelector defaults).
  2. Fetch each candidate's tape once (up to 2000 trades).
  3. Score each wallet's last-45d slice with compute_wallet_stats, plus
     efficiency = realized_pnl / gross USD volume.
  4. Build three top-8 portfolios:
       pmbot    : passes_filters -> rank_wallets(yaml weights) -> vet
       fomo     : 0.5*winrate + 0.5*efficiency (min-max normed), small
                  evidence floor only (>=10 trades, >=3 resolved markets)
       fomo-wr  : pure win-rate (tiebreak efficiency) — the "90%+ Winrate" bin
  5. ExactCopy-backtest each portfolio over the last 45d with production
     sizing; report side by side.

Caveats printed with results: selection and evaluation share the window
(in-sample for BOTH rules, so the comparison is fair even if the absolute
numbers are optimistic), and heavy traders' 2000-trade tapes may not span
the full 45d.

Usage:
    .venv/Scripts/python.exe -m scripts.compare_fomo
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

from pmbot.backtest import ExactCopyBacktester
from pmbot.config import Settings
from pmbot.data import GammaClient, PolymarketDataClient
from pmbot.leaders.config import load_leader_config
from pmbot.leaders.discovery import harvest_candidates
from pmbot.leaders.scoring import WalletStats, compute_wallet_stats, passes_filters, rank_wallets

WINDOW_DAYS = 45
TOP_N = 8
MAX_CANDIDATES = 200          # LeaderSelector default (alphabetical guard cap)
FOMO_MIN_TRADES = 10          # minimal evidence floor for the Fomo-style bins
FOMO_MIN_RESOLVED = 3
PACE_S = 0.15                 # gentle pacing between per-wallet tape fetches


def _settings() -> Settings:
    return Settings()          # production defaults are the tuned values


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    s = _settings()
    cfg = load_leader_config()
    data, gamma = PolymarketDataClient(), GammaClient()
    bt = ExactCopyBacktester(data, gamma, settings=s, trades_limit=2000)
    t0 = time.time()

    # -- 1) harvest the live engine's pool ---------------------------------
    print("harvesting candidate pool (top 30 markets x 100 wallets)...", flush=True)
    pool = harvest_candidates(data, gamma, top_markets=30, per_market=100)
    ordered = sorted(pool)
    if len(ordered) > MAX_CANDIDATES:
        ordered = ordered[:MAX_CANDIDATES]
    print(f"  {len(pool)} harvested, scoring {len(ordered)} (engine cap) "
          f"[{time.time()-t0:.0f}s]", flush=True)

    # -- 2) one tape per wallet --------------------------------------------
    tapes: dict[str, list] = {}
    for i, w in enumerate(ordered, 1):
        tapes.update(bt.fetch_tapes([w]))
        if i % 20 == 0:
            print(f"  tapes {i}/{len(ordered)} [{time.time()-t0:.0f}s]", flush=True)
        time.sleep(PACE_S)

    # -- 3) stats on the 45d slice (resolutions shared with the simulator) --
    def resolver(cid: str):
        m, winner = bt._market(cid)
        return (bool(m and m.closed), winner)

    stats: list[WalletStats] = []
    volume: dict[str, float] = {}
    for i, (w, tape) in enumerate(tapes.items(), 1):
        recent = [t for t in tape if t.timestamp >= cutoff]
        if not recent:
            continue
        stats.append(compute_wallet_stats(w, recent, resolver, now=now))
        volume[w] = sum(t.usd_size for t in recent)
        if i % 20 == 0:
            print(f"  stats {i}/{len(tapes)} [{time.time()-t0:.0f}s]", flush=True)
    print(f"  {len(stats)} wallets active in window [{time.time()-t0:.0f}s]", flush=True)

    def eff(st: WalletStats) -> float:
        v = volume.get(st.wallet, 0.0)
        return st.realized_pnl / v if v > 0 else 0.0

    # -- 4a) pmbot selection: filters -> weighted rank -> top 8 -> vet ------
    eligible = [st for st in stats if passes_filters(st, cfg.filters)]
    ranked = rank_wallets(eligible, cfg.weights)
    pmbot_pre = [r.wallet for r in ranked[: cfg.selection.top_n]]
    pmbot_kept: list[str] = []
    for w in pmbot_pre:
        m = bt.simulate({w: tapes[w]}, lookback_days=s.copy_vet_lookback_days,
                        min_leader_notional=s.copy_min_leader_notional_usd,
                        now=now).metrics()
        if m["n_trades"] == 0 or m["net_pnl"] >= s.copy_vet_min_pnl_usd:
            pmbot_kept.append(w)
        else:
            print(f"  vet DROPPED {w[:10]} (copy-pnl ${m['net_pnl']:.2f} "
                  f"over {m['n_trades']} trades)", flush=True)
    print(f"pmbot rule: {len(eligible)} eligible -> top {len(pmbot_pre)} "
          f"-> {len(pmbot_kept)} after vet", flush=True)

    # -- 4b) Fomo-style selections ------------------------------------------
    fomo_pool = [st for st in stats
                 if st.n_trades >= FOMO_MIN_TRADES
                 and st.n_resolved_markets >= FOMO_MIN_RESOLVED]

    def norm(vals: list[float]) -> list[float]:
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-12:
            return [0.5] * len(vals)
        return [(v - lo) / (hi - lo) for v in vals]

    wr_n = norm([st.win_rate for st in fomo_pool])
    ef_n = norm([eff(st) for st in fomo_pool])
    fomo_scored = sorted(zip(fomo_pool, wr_n, ef_n),
                         key=lambda x: -(0.5 * x[1] + 0.5 * x[2]))
    fomo8 = [st.wallet for st, _, _ in fomo_scored[:TOP_N]]
    fomo_wr8 = [st.wallet for st in sorted(fomo_pool,
                key=lambda st: (-st.win_rate, -eff(st)))[:TOP_N]]
    print(f"fomo pool: {len(fomo_pool)} wallets pass the evidence floor", flush=True)

    # -- member tables -------------------------------------------------------
    by_wallet = {st.wallet: st for st in stats}

    def table(name: str, members: list[str]) -> None:
        print(f"\n--- {name} ---")
        for w in members:
            st = by_wallet.get(w)
            if st is None:
                print(f"  {w}  (no window stats)")
                continue
            print(f"  {w}  n={st.n_trades:>4} resolved={st.n_resolved_markets:>3} "
                  f"win={st.win_rate*100:>5.1f}% pnl=${st.realized_pnl:>10,.2f} "
                  f"eff={eff(st)*100:>6.2f}% vol=${volume.get(w, 0):>11,.0f}")

    table("pmbot current config (post-vet)", pmbot_kept)
    table("fomo-style: winrate + efficiency", fomo8)
    table("fomo-style: pure winrate", fomo_wr8)

    # -- 5) same-window exact-copy backtests --------------------------------
    def run(name: str, members: list[str]) -> None:
        sub = {w: tapes[w] for w in members if w in tapes}
        m = bt.simulate(sub, lookback_days=WINDOW_DAYS,
                        min_leader_notional=s.copy_min_leader_notional_usd,
                        now=now).metrics()
        print(f"  {name:<34} n={m['n_trades']:>4} invested=${m['invested']:>9,.2f} "
              f"net=${m['net_pnl']:>9,.2f} roi={m['roi']*100:>6.1f}% "
              f"win={m['win_rate']*100:>5.1f}% mdd=${m['max_drawdown']:>8,.2f}")

    print(f"\n=== 45d exact-copy backtest (production sizing, ${s.bankroll_usd:.0f} "
          f"bankroll, {s.slippage_bps:.0f}bps slip) ===")
    run("pmbot current config (post-vet)", pmbot_kept)
    run("fomo: winrate + efficiency top-8", fomo8)
    run("fomo: pure winrate top-8", fomo_wr8)

    print("\ncaveats:")
    print("  - both rules select on the same window they're evaluated on "
          "(fair head-to-head, optimistic absolute numbers)")
    print("  - heavy traders' 2000-trade tapes may not reach back the full 45d")
    print("  - compare within this run only; absolute PnL isn't comparable "
          "across runs (pool/window drift)")
    print(f"\n(total {time.time()-t0:.0f}s)")
    data.close()
    gamma.close()


if __name__ == "__main__":
    main()
