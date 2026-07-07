"""Strategy #1 — cross-platform arbitrage (Polymarket vs Kalshi).

Submodules:
  fees     Kalshi fee model + net-edge math (pure functions)
  matcher  fuzzy cross-venue market matching + confirmed-pairs config
  scanner  live opportunity detection over confirmed pairs
"""

from pmbot.arb.fees import kalshi_taker_fee, pair_cost, pair_net_edge

__all__ = ["kalshi_taker_fee", "pair_cost", "pair_net_edge"]
