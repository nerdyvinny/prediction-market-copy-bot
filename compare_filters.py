#!/usr/bin/env python3
"""Compare leader counts under strict vs loose filters (copyable-trades scoring).

Reuses ONE discovery + stats pass, then applies different filter profiles to the
same WalletStats. This avoids re-running the slow scoring 5x.
"""
import sys
from datetime import datetime, timedelta, timezone

from pmbot.data import PolymarketDataClient, GammaClient
from pmbot.leaders.config import load_leader_config, FilterConfig
from pmbot.leaders.scoring import (
    compute_wallet_stats, passes_filters, rank_wallets,
)
from pmbot.leaders.discovery import harvest_candidates


def log(msg):
    print(msg, flush=True)


def main():
    cfg = load_leader_config()
    data = PolymarketDataClient()
    gamma = GammaClient()
    now = datetime.now(timezone.utc)

    resolution_memo = {}
    def resolver(cid):
        if cid in resolution_memo:
            return resolution_memo[cid]
        try:
            res = gamma.get_resolution(cid)
        except Exception:
            res = (False, None)
        resolution_memo[cid] = res
        return res

    log("Harvesting candidates...")
    candidates = harvest_candidates(data, gamma, top_markets=8, per_market=50)
    candidates -= set(cfg.blocklist)
    ordered = sorted(candidates)[:30]
    log(f"Got {len(ordered)} candidates to score (copyable-trades scoring ON).\n")

    cutoff = now - timedelta(days=90)
    stats = []
    for i, wallet in enumerate(ordered):
        try:
            trades = data.get_trades(user=wallet, limit=300)
        except Exception:
            continue
        trades = [t for t in trades if t.timestamp >= cutoff]
        if not trades:
            continue
        st = compute_wallet_stats(
            wallet, trades, resolver, now=now,
            copyable_trades_only=True, price_min=0.05, price_max=0.95,
        )
        stats.append(st)
        log(f"  [{i+1}/{len(ordered)}] {wallet[:12]} | copyable_trades={st.n_trades:3d} "
            f"resolved={st.n_resolved_markets:2d} P&L=${st.realized_pnl:8.2f} "
            f"win={st.win_rate*100:4.0f}% cats={st.n_categories}")

    log(f"\nScored {len(stats)} wallets with copyable trades.\n")

    # Apply filter profiles to the SAME stats. All now require >=5 resolved
    # markets (real track record) — rejects high-volume / no-outcome wallets.
    strict = FilterConfig(
        min_resolved_trades=100, min_resolved_markets=5, min_win_rate=0.55,
        min_distinct_categories=2, max_position_concentration=0.40,
    )
    loose = FilterConfig(
        min_resolved_trades=20, min_resolved_markets=5, min_win_rate=0.50,
        min_distinct_categories=1, max_position_concentration=0.50,
    )
    recommended = FilterConfig(
        min_resolved_trades=5, min_resolved_markets=5, min_win_rate=0.50,
        min_realized_pnl_usd=0, min_distinct_categories=1,
        max_position_concentration=0.60,
    )

    for name, flt, top_n in [
        ("STRICT (trades=100, markets=5, wr=0.55, cats=2)", strict, 10),
        ("LOOSE  (trades=20,  markets=5, wr=0.50, cats=1)", loose, 10),
        ("RECOMM (trades=5,   markets=5, wr=0.50, pnl>0)  ", recommended, 10),
    ]:
        eligible = [s for s in stats if passes_filters(s, flt)]
        ranked = rank_wallets(eligible, cfg.weights)[:top_n]
        log(f"=== {name} ===")
        log(f"    {len(eligible)} eligible -> top {len(ranked)}")
        for r in ranked:
            log(f"      {r.wallet[:12]} score={r.score:.3f} "
                f"P&L=${r.stats.realized_pnl:8.2f} win={r.stats.win_rate*100:4.0f}% "
                f"copyable_trades={r.stats.n_trades}")
        log("")


if __name__ == "__main__":
    main()
