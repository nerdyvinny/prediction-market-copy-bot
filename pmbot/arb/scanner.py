"""Live arbitrage scanner over human-confirmed cross-venue pairs.

For each ConfirmedPair the equivalence is:
    PM `pm_outcome` == Kalshi YES   (aligned=True)
    PM `pm_outcome` == Kalshi NO    (aligned=False)

Both hedge directions are checked each scan:
    A) buy the PM outcome        + buy the OPPOSITE Kalshi side
    B) buy the PM complement     + buy the EQUIVALENT Kalshi side

Either way exactly one leg pays $1/contract at resolution, so the position is
profitable when  ask_pm + ask_kalshi < 1 - kalshi_fee - slippage_buffer.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from pmbot.arb.fees import SizedEdge, size_pair
from pmbot.arb.matcher import ConfirmedPair
from pmbot.data import GammaClient, KalshiClient, PriceCache
from pmbot.models import KalshiMarket, Market

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArbOpportunity:
    """One executable two-leg opportunity, already sized to budget + depth."""

    pair: ConfirmedPair
    pm_market: Market
    kalshi_market: KalshiMarket
    pm_outcome: str        # outcome we buy on Polymarket
    pm_token_id: str
    pm_ask: float
    kalshi_side: str       # "yes" | "no" we buy on Kalshi
    kalshi_ask: float
    sized: SizedEdge
    net_edge_per_pair: float   # after fee + slippage buffer

    @property
    def uid(self) -> str:
        """Dedupe id: one open arb entry per pair+direction."""
        return f"arb:{self.pair.uid}:{self.pm_outcome}"

    def describe(self) -> str:
        return (
            f"BUY PM[{self.pm_outcome}] @ {self.pm_ask:.3f} + "
            f"K[{self.kalshi_side.upper()}:{self.kalshi_market.ticker}] @ {self.kalshi_ask:.3f}"
            f" -> {self.sized.contracts:.0f} pairs, cost ${self.sized.cost_usd:.2f}"
            f" (fee ${self.sized.fee_usd:.2f}), locked profit ${self.sized.profit_usd:.2f}"
            f" ({self.net_edge_per_pair*100:.2f}%/pair net)"
        )


class ArbScanner:
    def __init__(
        self,
        gamma: GammaClient,
        price_cache: PriceCache,
        kalshi: KalshiClient,
        *,
        min_edge: float = 0.015,
        slippage_buffer: float = 0.005,
        max_per_trade_usd: float = 50.0,
        max_workers: int = 8,
    ):
        self.gamma = gamma
        self.prices = price_cache
        self.kalshi = kalshi
        self.min_edge = min_edge
        self.slippage_buffer = slippage_buffer
        self.max_per_trade_usd = max_per_trade_usd
        self.max_workers = max_workers
        self.last_scan_seconds: float = 0.0

    def scan(self, pairs: list[ConfirmedPair]) -> list[ArbOpportunity]:
        """Check every confirmed pair in both directions, concurrently.

        Latency is edge in arbitrage: pairs are scanned in parallel (each
        needs 3 HTTP calls) so 20 pairs take ~1 round-trip, not 20. Per-pair
        failures are isolated so one dead market can't kill the scan.
        """
        if not pairs:
            return []
        t0 = time.monotonic()
        out: list[ArbOpportunity] = []
        workers = max(1, min(self.max_workers, len(pairs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(self._scan_pair_safe, pairs):
                out.extend(result)
        self.last_scan_seconds = time.monotonic() - t0
        out.sort(key=lambda o: -o.sized.profit_usd)
        return out

    def _scan_pair_safe(self, pair: ConfirmedPair) -> list[ArbOpportunity]:
        try:
            return self._scan_pair(pair)
        except Exception as e:
            log.warning("arb scan failed for %s: %s", pair.kalshi_ticker, e)
            return []

    def _scan_pair(self, pair: ConfirmedPair) -> list[ArbOpportunity]:
        pm = self.gamma.get_market(pair.pm_market_id)
        if pm is None or pm.closed:
            log.debug("arb: PM market %s missing/closed", pair.pm_market_id[:12])
            return []
        k = self.kalshi.get_market(pair.kalshi_ticker)
        if k is None or not k.is_open:
            log.debug("arb: Kalshi market %s missing/closed", pair.kalshi_ticker)
            return []

        pm_outcome = pair.pm_outcome
        complement = _complement_outcome(pm, pm_outcome)
        if complement is None:
            log.warning("arb: %s is not a binary Yes/No market; skipping", pm.market_id[:12])
            return []

        # Kalshi side equivalent to the PM outcome, and its opposite.
        k_equiv = "yes" if pair.aligned else "no"
        k_opp = "no" if pair.aligned else "yes"

        found: list[ArbOpportunity] = []
        # A) PM outcome + opposite Kalshi side.
        opp = self._check_direction(pair, pm, k, pm_outcome, k_opp)
        if opp:
            found.append(opp)
        # B) PM complement + equivalent Kalshi side.
        opp = self._check_direction(pair, pm, k, complement, k_equiv)
        if opp:
            found.append(opp)
        return found

    def _check_direction(
        self,
        pair: ConfirmedPair,
        pm: Market,
        k: KalshiMarket,
        pm_outcome: str,
        kalshi_side: str,
    ) -> ArbOpportunity | None:
        token_id = pm.tokens.get(pm_outcome)
        if not token_id:
            return None
        quote = self.prices.get_quote(token_id)
        pm_ask, pm_depth = quote.ask, quote.ask_size
        k_ask = k.yes_ask if kalshi_side == "yes" else k.no_ask
        k_depth = k.yes_ask_size if kalshi_side == "yes" else k.no_ask_size
        if pm_ask is None or k_ask is None:
            return None

        gross = 1.0 - pm_ask - k_ask
        if gross <= 0:
            return None

        max_contracts = min(
            pm_depth if pm_depth is not None else float("inf"),
            k_depth if k_depth is not None else float("inf"),
        )
        sized = size_pair(
            pm_ask, k_ask,
            max_usd=self.max_per_trade_usd,
            max_contracts=None if max_contracts == float("inf") else max_contracts,
        )
        if sized is None:
            return None
        net = sized.edge_per_pair - self.slippage_buffer
        if net < self.min_edge:
            return None
        return ArbOpportunity(
            pair=pair,
            pm_market=pm,
            kalshi_market=k,
            pm_outcome=pm_outcome,
            pm_token_id=token_id,
            pm_ask=pm_ask,
            kalshi_side=kalshi_side,
            kalshi_ask=k_ask,
            sized=sized,
            net_edge_per_pair=round(net, 6),
        )


def _complement_outcome(market: Market, outcome: str) -> str | None:
    """The other outcome in a binary market, or None if not binary."""
    others = [o for o in market.tokens if o != outcome]
    return others[0] if len(others) == 1 and outcome in market.tokens else None
