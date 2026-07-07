"""Fee + edge math for cross-platform arbitrage. Pure functions.

The trade: buy YES on one venue at price a, buy NO on the other at price b.
At resolution exactly one leg pays $1 per contract, so gross profit per
contract pair is 1 - (a + b). It's only real profit after:

  - Kalshi taker fee: ceil_to_cent(0.07 * C * P * (1-P)) per order
    (max 1.75c/contract at P=0.50; Kalshi rounds the ORDER total up, so
    small orders pay proportionally more)
  - Polymarket: no trading fee (gas is ~negligible and covered by the
    slippage buffer)
  - a slippage buffer for the books moving between scan and fill

Prices are dollars in (0, 1); sizes are contracts (1 contract pays $1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

KALSHI_TAKER_RATE = 0.07


def kalshi_taker_fee(contracts: float, price: float, rate: float = KALSHI_TAKER_RATE) -> float:
    """Kalshi trading fee for one taker order, rounded UP to the next cent.

    fee = ceil_to_cent(rate * contracts * price * (1 - price))
    """
    if contracts <= 0 or not (0.0 < price < 1.0):
        return 0.0
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def pair_cost(
    contracts: float,
    pm_price: float,
    kalshi_price: float,
) -> float:
    """Total cost of `contracts` pairs: PM leg + Kalshi leg + Kalshi fee."""
    return (
        contracts * pm_price
        + contracts * kalshi_price
        + kalshi_taker_fee(contracts, kalshi_price)
    )


def pair_net_edge(
    contracts: float,
    pm_price: float,
    kalshi_price: float,
    slippage_buffer: float = 0.0,
) -> float:
    """Net profit per contract pair at resolution, after fees + buffer.

    Payout is exactly $1 per pair (one leg wins), so:
      edge/pair = 1 - pm_price - kalshi_price - fee/contract - buffer
    Returns 0.0 edge for non-positive sizes.
    """
    if contracts <= 0:
        return 0.0
    payout = contracts * 1.0
    profit = payout - pair_cost(contracts, pm_price, kalshi_price) - contracts * slippage_buffer
    return profit / contracts


@dataclass(frozen=True)
class SizedEdge:
    """Result of sizing an opportunity to a budget + depth constraints."""

    contracts: float
    cost_usd: float          # total outlay including Kalshi fee
    fee_usd: float           # Kalshi taker fee for the order
    profit_usd: float        # guaranteed profit at resolution (pre-buffer)
    edge_per_pair: float     # profit_usd / contracts


def size_pair(
    pm_price: float,
    kalshi_price: float,
    *,
    max_usd: float,
    max_contracts: float | None = None,
    min_contracts: float = 1.0,
) -> SizedEdge | None:
    """Choose the largest whole-contract size within budget and depth.

    Returns None if even `min_contracts` doesn't fit the budget or the edge
    is non-positive at that size (fee rounding can kill tiny orders).
    """
    per_pair = pm_price + kalshi_price
    if per_pair <= 0 or per_pair >= 1.0 or max_usd <= 0:
        return None

    # Upper bound ignoring the fee, then walk down until total cost fits.
    n = math.floor(max_usd / per_pair)
    if max_contracts is not None:
        n = min(n, math.floor(max_contracts))
    while n >= min_contracts:
        cost = pair_cost(n, pm_price, kalshi_price)
        if cost <= max_usd:
            fee = kalshi_taker_fee(n, kalshi_price)
            profit = n * 1.0 - cost
            if profit <= 0:
                return None
            return SizedEdge(
                contracts=float(n),
                cost_usd=round(cost, 4),
                fee_usd=fee,
                profit_usd=round(profit, 4),
                edge_per_pair=round(profit / n, 6),
            )
        n -= 1
    return None
