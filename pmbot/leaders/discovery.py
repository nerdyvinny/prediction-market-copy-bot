"""Leader discovery: harvest candidate wallets to score.

Polymarket's leaderboard endpoint isn't publicly documented/stable, so instead
of relying on it we build our own candidate pool: take the most active markets
(Gamma), then collect the wallets that hold or recently traded them (Data API).
Those candidates are then scored in `scoring.py`.
"""

from __future__ import annotations

import logging

from pmbot.data import GammaClient, PolymarketDataClient

log = logging.getLogger(__name__)


def harvest_candidates(
    data: PolymarketDataClient,
    gamma: GammaClient,
    *,
    top_markets: int = 8,
    per_market: int = 50,
) -> set[str]:
    """Return a set of candidate wallet addresses (lowercased)."""
    wallets: set[str] = set()
    try:
        markets = gamma.get_markets(limit=top_markets)
    except Exception as e:
        log.warning("discovery: failed to list markets: %s", e)
        return wallets

    for m in markets:
        if not m.market_id:
            continue
        try:
            for w in data.harvest_wallets_from_market(m.market_id, limit=per_market):
                if w:
                    wallets.add(w.lower())
        except Exception as e:
            log.debug("discovery: harvest failed for %s: %s", m.market_id[:12], e)
            continue

    log.info("discovery: harvested %d candidate wallets from %d markets",
             len(wallets), len(markets))
    return wallets
