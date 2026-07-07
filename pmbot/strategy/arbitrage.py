"""Strategy #1 — cross-platform arbitrage (Polymarket vs Kalshi).

Scans human-confirmed market pairs (pmbot/config/arb_pairs.yaml) for moments
where buying complementary sides across venues costs < $1 after Kalshi fees
and a slippage buffer. Emits both legs as Signals sharing a `leg_group` so
the engine executes them both-or-neither.

Signals are emitted PRE-SIZED (contracts already fixed by budget + book
depth); the RiskManager only applies portfolio-level caps, never
copy_fraction scaling.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from pmbot.arb.matcher import ConfirmedPair, load_pairs_config
from pmbot.arb.scanner import ArbOpportunity, ArbScanner
from pmbot.config import get_settings
from pmbot.data import GammaClient, KalshiClient, PriceCache
from pmbot.models import Side, Signal, Venue
from pmbot.portfolio.ledger import Ledger
from pmbot.strategy.base import Strategy

log = logging.getLogger(__name__)


def kalshi_token_id(ticker: str, side: str) -> str:
    """Synthetic token id for a Kalshi contract side, ledger-unique."""
    return f"kalshi:{ticker}:{side.lower()}"


class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    def __init__(
        self,
        gamma: GammaClient,
        price_cache: PriceCache,
        kalshi: KalshiClient,
        ledger: Ledger,
        *,
        pairs: list[ConfirmedPair] | None = None,
        scanner: ArbScanner | None = None,
    ):
        s = get_settings()
        self.ledger = ledger
        self._explicit_pairs = pairs
        self._pairs_path = s.arb_pairs_path
        self.scanner = scanner or ArbScanner(
            gamma,
            price_cache,
            kalshi,
            min_edge=s.arb_min_edge,
            slippage_buffer=s.arb_slippage_buffer,
            max_per_trade_usd=s.arb_max_per_trade_usd,
        )

    def pairs(self) -> list[ConfirmedPair]:
        """Confirmed pairs, re-read each cycle so YAML edits apply live."""
        if self._explicit_pairs is not None:
            return self._explicit_pairs
        return load_pairs_config(self._pairs_path).pairs

    def generate(self) -> Iterable[Signal]:
        pairs = self.pairs()
        if not pairs:
            return
        for opp in self.scanner.scan(pairs):
            if self.ledger.has_copied(opp.uid):
                continue                      # one open entry per pair+direction
            yield from self._signals_for(opp)

    def _signals_for(self, opp: ArbOpportunity) -> Iterable[Signal]:
        group = f"{opp.uid}:{uuid.uuid4().hex[:8]}"
        n = opp.sized.contracts
        reason = (
            f"arb {opp.net_edge_per_pair*100:.2f}%/pair "
            f"({opp.pm_ask:.3f}+{opp.kalshi_ask:.3f}, fee ${opp.sized.fee_usd:.2f})"
        )
        yield Signal(
            market_id=opp.pm_market.market_id,
            token_id=opp.pm_token_id,
            outcome=opp.pm_outcome,
            side=Side.BUY,
            target_price=opp.pm_ask,
            size_usd=round(n * opp.pm_ask, 2),
            reason=reason,
            source_uid=opp.uid,
            venue=Venue.POLYMARKET.value,
            leg_group=group,
        )
        yield Signal(
            market_id=opp.kalshi_market.ticker,
            token_id=kalshi_token_id(opp.kalshi_market.ticker, opp.kalshi_side),
            outcome=opp.kalshi_side.upper(),
            side=Side.BUY,
            target_price=opp.kalshi_ask,
            size_usd=round(n * opp.kalshi_ask, 2),
            reason=reason,
            source_uid=opp.uid,
            venue=Venue.KALSHI.value,
            leg_group=group,
        )
